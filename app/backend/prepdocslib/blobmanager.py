import datetime
import io
import logging
import os
import re
from enum import Enum
from typing import List, Optional, Union

import fitz  # type: ignore
from azure.core.credentials_async import AsyncTokenCredential
from azure.storage.blob import (
    BlobClient,
    BlobSasPermissions,
    UserDelegationKey,
    generate_blob_sas,
)
from azure.storage.blob.aio import BlobServiceClient, ContainerClient
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

from .listfilestrategy import File

logger = logging.getLogger("scripts")


class BlobManager:
    """
    Class to manage uploading and deleting blobs containing citation information from a blob storage account
    """

    def __init__(
        self,
        endpoint: str,
        container: str,
        account: str,
        credential: Union[AsyncTokenCredential, str],
        resourceGroup: str,
        subscriptionId: str,
        store_page_images: bool = False,
    ):
        self.endpoint = endpoint
        self.credential = credential
        self.account = account
        self.container = container
        self.store_page_images = store_page_images
        self.resourceGroup = resourceGroup
        self.subscriptionId = subscriptionId
        self.user_delegation_key: Optional[UserDelegationKey] = None

    async def _create_new_blob(self, file: File, container_client: ContainerClient) -> BlobClient:
        with open(file.content.name, "rb") as reopened_file:
            blob_name = BlobManager.blob_name_from_file_name(file.content.name)
            logger.info("Uploading blob for whole file -> %s", blob_name)
            return await container_client.upload_blob(blob_name, reopened_file, overwrite=True, metadata=file.metadata)

    async def _file_blob_update_needed(self, blob_client: BlobClient, file: File) -> bool:
        # Get existing blob properties
        blob_properties = await blob_client.get_blob_properties()
        blob_metadata = blob_properties.metadata

        # Check if the md5 values are the same
        file_md5 = file.metadata.get("md5")
        blob_md5 = blob_metadata.get("md5")

        # If the file has an md5 value, check if it is different from the blob
        return file_md5 and file_md5 != blob_md5

    async def upload_blob(self, file: File) -> Optional[List[str]]:
        async with BlobServiceClient(
            account_url=self.endpoint, credential=self.credential, max_single_put_size=4 * 1024 * 1024
        ) as service_client, service_client.get_container_client(self.container) as container_client:
            if not await container_client.exists():
                await container_client.create_container()

            # Re-open and upload the original file if the blob does not exist or the md5 values do not match
            class MD5Check(Enum):
                NOT_DONE = 0
                MATCH = 1
                NO_MATCH = 2

            md5_check = MD5Check.NOT_DONE

            # Upload the file to Azure Storage
            # file.url is only None if files are not uploaded yet, for datalake it is set
            if file.url is None:
                blob_client = container_client.get_blob_client(file.url)

                if not await blob_client.exists():
                    logger.info("Blob %s does not exist, uploading", file.url)
                    blob_client = await self._create_new_blob(file, container_client)
                else:
                    if self._blob_update_needed(blob_client, file):
                        logger.info("Blob %s exists but md5 values do not match, updating", file.url)
                        md5_check = MD5Check.NO_MATCH
                        # Upload the file with the updated metadata
                        with open(file.content.name, "rb") as data:
                            await blob_client.upload_blob(data, overwrite=True, metadata=file.metadata)
                    else:
                        logger.info("Blob %s exists and md5 values match, skipping upload", file.url)
                        md5_check = MD5Check.MATCH
                file.url = blob_client.url

            if md5_check != MD5Check.MATCH and self.store_page_images:
                if os.path.splitext(file.content.name)[1].lower() == ".pdf":
                    return await self.upload_pdf_blob_images(service_client, container_client, file)
                else:
                    logger.info("File %s is not a PDF, skipping image upload", file.content.name)

    def get_managedidentity_connectionstring(self):
        return f"ResourceId=/subscriptions/{self.subscriptionId}/resourceGroups/{self.resourceGroup}/providers/Microsoft.Storage/storageAccounts/{self.account};"

    async def upload_pdf_blob_images(
        self, service_client: BlobServiceClient, container_client: ContainerClient, file: File
    ) -> List[str]:
        with open(file.content.name, "rb") as reopened_file:
            reader = PdfReader(reopened_file)
            page_count = len(reader.pages)
        doc = fitz.open(file.content.name)
        sas_uris = []
        start_time = datetime.datetime.now(datetime.timezone.utc)
        expiry_time = start_time + datetime.timedelta(days=1)

        font = None
        try:
            font = ImageFont.truetype("arial.ttf", 20)
        except OSError:
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/freefont/FreeMono.ttf", 20)
            except OSError:
                logger.info("Unable to find arial.ttf or FreeMono.ttf, using default font")

        for i in range(page_count):
            blob_name = BlobManager.blob_image_name_from_file_page(file.content.name, i)

            blob_client = container_client.get_blob_client(blob_name)
            if await blob_client.exists():
                # Get existing blob properties
                blob_properties = await blob_client.get_blob_properties()
                blob_metadata = blob_properties.metadata

                # Check if the md5 values are the same
                file_md5 = file.metadata.get("md5")
                blob_md5 = blob_metadata.get("md5")
                if file_md5 == blob_md5:
                    logger.info("Blob %s exists and md5 values match, skipping upload", blob_name)
                    continue  #  document already uploaded

            logger.info("Converting page %s to image and uploading -> %s", i, blob_name)

            doc = fitz.open(file.content.name)
            page = doc.load_page(i)
            pix = page.get_pixmap()
            original_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)  # type: ignore

            # Create a new image with additional space for text
            text_height = 40  # Height of the text area
            new_img = Image.new("RGB", (original_img.width, original_img.height + text_height), "white")

            # Paste the original image onto the new image
            new_img.paste(original_img, (0, text_height))

            # Draw the text on the white area
            draw = ImageDraw.Draw(new_img)
            text = f"SourceFileName:{blob_name}"

            # 10 pixels from the top and left of the image
            x = 10
            y = 10
            draw.text((x, y), text, font=font, fill="black")

            output = io.BytesIO()
            new_img.save(output, format="PNG")
            output.seek(0)

            await blob_client.upload_blob(data=output, overwrite=True, metadata=file.metadata)
            if not self.user_delegation_key:
                self.user_delegation_key = await service_client.get_user_delegation_key(start_time, expiry_time)

            if container_client.account_name is not None:
                sas_token = generate_blob_sas(
                    account_name=container_client.account_name,
                    container_name=container_client.container_name,
                    blob_name=blob_name,
                    user_delegation_key=self.user_delegation_key,
                    permission=BlobSasPermissions(read=True),
                    expiry=expiry_time,
                    start=start_time,
                )
                sas_uris.append(f"{blob_client.url}?{sas_token}")

        return sas_uris

    async def remove_blob(self, path: Optional[str] = None):
        async with BlobServiceClient(
            account_url=self.endpoint, credential=self.credential
        ) as service_client, service_client.get_container_client(self.container) as container_client:
            if not await container_client.exists():
                return
            if path is None:
                prefix = None
                blobs = container_client.list_blob_names()
            else:
                prefix = os.path.splitext(os.path.basename(path))[0]
                blobs = container_client.list_blob_names(name_starts_with=os.path.splitext(os.path.basename(prefix))[0])
            async for blob_path in blobs:
                # This still supports PDFs split into individual pages, but we could remove in future to simplify code
                if (
                    prefix is not None
                    and (
                        not re.match(rf"{prefix}-\d+\.pdf", blob_path) or not re.match(rf"{prefix}-\d+\.png", blob_path)
                    )
                ) or (path is not None and blob_path == os.path.basename(path)):
                    continue
                logger.info("Removing blob %s", blob_path)
                await container_client.delete_blob(blob_path)

    @classmethod
    def sourcepage_from_file_page(cls, filename, page=0) -> str:
        if os.path.splitext(filename)[1].lower() == ".pdf":
            return f"{os.path.basename(filename)}#page={page+1}"
        else:
            return os.path.basename(filename)

    @classmethod
    def blob_image_name_from_file_page(cls, filename, page=0) -> str:
        return os.path.splitext(os.path.basename(filename))[0] + f"-{page+1}" + ".png"

    @classmethod
    def blob_name_from_file_name(cls, filename) -> str:
        return os.path.basename(filename)
