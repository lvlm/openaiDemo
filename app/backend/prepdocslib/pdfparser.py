import html
import io
import json
import logging
from enum import Enum
from typing import IO, AsyncGenerator, Union

import pymupdf
from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import (
    AnalyzeDocumentRequest,
    AnalyzeResult,
    DocumentFigure,
    DocumentTable,
)
from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from PIL import Image
from pypdf import PdfReader

from .cu_image import ContentUnderstandingManager
from .page import Page
from .parser import Parser

logger = logging.getLogger("scripts")


class LocalPdfParser(Parser):
    """
    Concrete parser backed by PyPDF that can parse PDFs into pages
    To learn more, please visit https://pypi.org/project/pypdf/
    """

    async def parse(self, content: IO) -> AsyncGenerator[Page, None]:
        logger.info("Extracting text from '%s' using local PDF parser (pypdf)", content.name)

        reader = PdfReader(content)
        pages = reader.pages
        offset = 0
        for page_num, p in enumerate(pages):
            page_text = p.extract_text()
            yield Page(page_num=page_num, offset=offset, text=page_text)
            offset += len(page_text)


class DocumentAnalysisParser(Parser):
    """
    Concrete parser backed by Azure AI Document Intelligence that can parse many document formats into pages
    To learn more, please visit https://learn.microsoft.com/azure/ai-services/document-intelligence/overview
    """

    def __init__(
        self,
        endpoint: str,
        credential: Union[AsyncTokenCredential, AzureKeyCredential],
        model_id="prebuilt-layout",
        use_content_understanding=True,
        content_understanding_endpoint: str = None,
    ):
        self.model_id = model_id
        self.endpoint = endpoint
        self.credential = credential
        self.use_content_understanding = use_content_understanding
        self.content_understanding_endpoint = content_understanding_endpoint

    async def parse(self, content: IO) -> AsyncGenerator[Page, None]:
        logger.info("Extracting text from '%s' using Azure Document Intelligence", content.name)

        cu_manager = ContentUnderstandingManager(self.content_understanding_endpoint, self.credential)
        async with DocumentIntelligenceClient(
            endpoint=self.endpoint, credential=self.credential
        ) as document_intelligence_client:
            # turn content into bytes
            content_bytes = content.read()
            if self.use_content_understanding:
                poller = await document_intelligence_client.begin_analyze_document(
                    model_id="prebuilt-layout",
                    analyze_request=AnalyzeDocumentRequest(bytes_source=content_bytes),
                    output=["figures"],
                    features=["ocrHighResolution"],
                    output_content_format="markdown",
                )
                doc_for_pymupdf = pymupdf.open(stream=io.BytesIO(content_bytes))
            else:
                poller = await document_intelligence_client.begin_analyze_document(
                    model_id=self.model_id, analyze_request=content, content_type="application/octet-stream"
                )
            form_recognizer_results: AnalyzeResult = await poller.result()

            offset = 0
            pages_json = []
            for page in form_recognizer_results.pages:
                tables_on_page = [
                    table
                    for table in (form_recognizer_results.tables or [])
                    if table.bounding_regions and table.bounding_regions[0].page_number == page.page_number
                ]
                figures_on_page = []
                if self.use_content_understanding:
                    figures_on_page = [
                        figure
                        for figure in (form_recognizer_results.figures or [])
                        if figure.bounding_regions and figure.bounding_regions[0].page_number == page.page_number
                    ]

                class ObjectType(Enum):
                    NONE = -1
                    TABLE = 0
                    FIGURE = 1

                # mark all positions of the table spans in the page
                page_offset = page.spans[0].offset
                page_length = page.spans[0].length
                mask_chars = [(ObjectType.NONE, None)] * page_length
                for table_idx, table in enumerate(tables_on_page):
                    for span in table.spans:
                        # replace all table spans with "table_id" in table_chars array
                        for i in range(span.length):
                            idx = span.offset - page_offset + i
                            if idx >= 0 and idx < page_length:
                                mask_chars[idx] = (ObjectType.TABLE, table_idx)
                for figure_idx, figure in enumerate(figures_on_page):
                    for span in figure.spans:
                        # replace all figure spans with "figure_id" in figure_chars array
                        for i in range(span.length):
                            idx = span.offset - page_offset + i
                            if idx >= 0 and idx < page_length:
                                mask_chars[idx] = (ObjectType.FIGURE, figure_idx)

                # build page text by replacing characters in table spans with table html
                page_text = ""
                added_objects = set()  # set of object types todo mypy
                for idx, mask_char in enumerate(mask_chars):
                    object_type, object_idx = mask_char
                    if object_type == ObjectType.NONE:
                        page_text += form_recognizer_results.content[page_offset + idx]
                    elif object_type == ObjectType.TABLE:
                        if mask_char not in added_objects:
                            page_text += DocumentAnalysisParser.table_to_html(tables_on_page[object_idx])
                            added_objects.add(mask_char)
                    elif object_type == ObjectType.FIGURE:
                        if mask_char not in added_objects:
                            figure_html = await DocumentAnalysisParser.figure_to_html(
                                doc_for_pymupdf, cu_manager, figures_on_page[object_idx]
                            )
                            page_text += figure_html
                            added_objects.add(mask_char)
                # We remove these comments since they are not needed and skew the page numbers
                page_text = page_text.replace("<!-- PageBreak -->", "")
                # We remove excess newlines at the beginning and end of the page
                page_text = page_text.strip()
                yield Page(page_num=page.page_number - 1, offset=offset, text=page_text)
                # Serialize the page text to a JSON and save it locally
                page_json = {
                    "page_num": page.page_number - 1,
                    "offset": offset,
                    "text": page_text,
                }
                pages_json.append(page_json)
                offset += len(page_text)
            with open("pages.json", "w") as f:
                json.dump(pages_json, f)

    @staticmethod
    async def figure_to_html(
        doc: pymupdf.Document, cu_manager: ContentUnderstandingManager, figure: DocumentFigure
    ) -> str:
        logger.info("Describing figure '%s'", figure.id)
        for region in figure.bounding_regions:
            # To learn more about bounding regions, see https://aka.ms/bounding-region
            bounding_box = (
                region.polygon[0],  # x0 (left)
                region.polygon[1],  # y0 (top
                region.polygon[4],  # x1 (right)
                region.polygon[5],  # y1 (bottom)
            )
        page_number = figure.bounding_regions[0]["pageNumber"]  # 1-indexed
        cropped_img = DocumentAnalysisParser.crop_image_from_pdf_page(doc, page_number - 1, bounding_box)
        figure_description = await cu_manager.describe_image(cropped_img)
        figure_title = (figure.caption and figure.caption.content) or ""
        return f"<figure><figcaption>{figure_title}<br>{figure_description}</figcaption></figure>"

    @staticmethod
    def table_to_html(table: DocumentTable):
        table_html = "<figure><table>"
        rows = [
            sorted([cell for cell in table.cells if cell.row_index == i], key=lambda cell: cell.column_index)
            for i in range(table.row_count)
        ]
        for row_cells in rows:
            table_html += "<tr>"
            for cell in row_cells:
                tag = "th" if (cell.kind == "columnHeader" or cell.kind == "rowHeader") else "td"
                cell_spans = ""
                if cell.column_span is not None and cell.column_span > 1:
                    cell_spans += f" colSpan={cell.column_span}"
                if cell.row_span is not None and cell.row_span > 1:
                    cell_spans += f" rowSpan={cell.row_span}"
                table_html += f"<{tag}{cell_spans}>{html.escape(cell.content)}</{tag}>"
            table_html += "</tr>"
        table_html += "</table></figure>"
        return table_html

    @staticmethod
    def crop_image_from_pdf_page(doc: pymupdf.Document, page_number, bounding_box) -> bytes:
        """
        Crops a region from a given page in a PDF and returns it as an image.

        :param pdf_path: Path to the PDF file.
        :param page_number: The page number to crop from (0-indexed).
        :param bounding_box: A tuple of (x0, y0, x1, y1) coordinates for the bounding box.
        :return: A PIL Image of the cropped area.
        """
        page = doc.load_page(page_number)

        # Cropping the page. The rect requires the coordinates in the format (x0, y0, x1, y1).
        bbx = [x * 72 for x in bounding_box]
        rect = pymupdf.Rect(bbx)
        # Bounding box is scaled to 72 dots per inch
        # We assume the PDF has 300 DPI
        # The matrix is used to convert between these 2 units
        pix = page.get_pixmap(matrix=pymupdf.Matrix(300 / 72, 300 / 72), clip=rect)

        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        bytes_io = io.BytesIO()
        img.save(bytes_io, format="PNG")
        return bytes_io.getvalue()
