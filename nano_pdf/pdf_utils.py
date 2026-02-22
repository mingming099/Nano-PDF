import os
import subprocess
import shutil
from pdf2image import convert_from_path
from pypdf import PdfReader, PdfWriter, PageObject, Transformation
import pytesseract
from PIL import Image


def check_system_dependencies():
    """Checks if required system dependencies are installed."""
    missing = []

    # Check for pdftotext (part of poppler-utils)
    if not shutil.which('pdftotext'):
        missing.append('pdftotext (poppler/poppler-utils)')

    # Check for tesseract
    if not shutil.which('tesseract'):
        missing.append('tesseract')

    if missing:
        deps_str = ", ".join(missing)
        if os.name == 'darwin':  # macOS
            install_cmd = "brew install poppler tesseract"
        elif os.name == 'posix':  # Linux
            install_cmd = "sudo apt-get install poppler-utils tesseract-ocr"
        else:  # Windows
            install_cmd = "choco install poppler tesseract\n(You may need to restart your terminal after installation)"

        raise RuntimeError(
            f"Missing system dependencies: {deps_str}\n\n"
            f"Installation:\n{install_cmd}\n\n"
            f"See https://github.com/gavrielc/Nano-PDF#readme for more details."
        )


def get_page_count(pdf_path: str) -> int:
    """Returns the total number of pages in the PDF."""
    reader = PdfReader(pdf_path)
    return len(reader.pages)


def extract_full_text(pdf_path: str) -> str:
    """Extracts the full text from a PDF using pdftotext (via subprocess for speed/layout)."""
    try:
        # Using -layout to preserve some spatial structure which is good for slides
        result = subprocess.run(
            ['pdftotext', '-layout', pdf_path, '-'],
            capture_output=True,
            text=True,
            check=True
        )
        raw_text = result.stdout

        # Split by form feed to get pages
        pages = raw_text.split('\f')

        formatted_pages = []
        for i, page_text in enumerate(pages):
            # Skip empty pages at the end if any
            if not page_text.strip():
                continue

            # Strip whitespace
            clean_text = page_text.strip()

            # Truncate to 2000 chars
            if len(clean_text) > 2000:
                clean_text = clean_text[:2000] + "...[truncated]"

            # Wrap in page tags (1-indexed)
            formatted_pages.append(f"<page-{i+1}>\n{clean_text}\n</page-{i+1}>")

        return "<document_context>\n" + "\n".join(formatted_pages) + "\n</document_context>"
    except subprocess.CalledProcessError as e:
        print(f"Error extracting text: {e}")
        return ""


def render_page_as_image(pdf_path: str, page_number: int) -> Image.Image:
    """Renders a specific page (1-indexed) as a PIL Image."""
    images = convert_from_path(
        pdf_path,
        first_page=page_number,
        last_page=page_number
    )
    if not images:
        raise ValueError(f"Could not render page {page_number}")
    return images[0]


def rehydrate_image_to_pdf(image: Image.Image, output_pdf_path: str):
    """
    Converts an image to a single-page PDF with a hidden text layer using Tesseract.
    This is the 'State Preservation' step.
    """
    pdf_bytes = pytesseract.image_to_pdf_or_hocr(image, extension='pdf')
    with open(output_pdf_path, 'wb') as f:
        f.write(pdf_bytes)


def _page_rotation(page) -> int:
    """Return normalized page rotation in degrees."""
    try:
        rot = int(getattr(page, "rotation", 0) or 0)
    except Exception:
        try:
            rot = int(page.get('/Rotate', 0) or 0)
        except Exception:
            rot = 0
    rot %= 360
    return rot


def _fit_page_with_padding(source_page, target_width: float, target_height: float):
    """Fit page into target size while preserving aspect ratio (no stretching)."""
    src_w = float(source_page.mediabox.width)
    src_h = float(source_page.mediabox.height)

    if src_w <= 0 or src_h <= 0:
        canvas = PageObject.create_blank_page(width=target_width, height=target_height)
        canvas.merge_page(source_page)
        return canvas

    scale = min(target_width / src_w, target_height / src_h)
    scaled_w = src_w * scale
    scaled_h = src_h * scale
    tx = (target_width - scaled_w) / 2
    ty = (target_height - scaled_h) / 2

    canvas = PageObject.create_blank_page(width=target_width, height=target_height)
    source_page.add_transformation(Transformation().scale(scale).translate(tx, ty))
    canvas.merge_page(source_page)
    return canvas


def _build_replacement_page(original_page, new_page):
    """
    Build replacement page preserving *visual* orientation and aspect ratio.

    Important: for originals using /Rotate 90|270, we normalize to rotation=0 and
    swap canvas dimensions so viewers render naturally without requiring device
    rotation and without squeeze artifacts.
    """
    target_w = float(original_page.mediabox.width)
    target_h = float(original_page.mediabox.height)
    rot = _page_rotation(original_page)

    # Preserve visual orientation (what users actually see), not raw mediabox.
    if rot in (90, 270):
        visual_w, visual_h = target_h, target_w
    else:
        visual_w, visual_h = target_w, target_h

    result = _fit_page_with_padding(new_page, visual_w, visual_h)

    # Intentionally normalize to rotation=0 to avoid double-rotation behavior
    # across PDF viewers and mobile apps.
    return result


def replace_page_in_pdf(original_pdf_path: str, new_page_pdf_path: str, page_number: int, output_pdf_path: str):
    """
    Replaces a specific page in the original PDF with the new single-page PDF.
    page_number is 1-indexed.
    """
    reader = PdfReader(original_pdf_path)
    writer = PdfWriter()

    for i in range(len(reader.pages)):
        if i == page_number - 1:
            original_page = reader.pages[i]
            new_reader = PdfReader(new_page_pdf_path)
            new_page = new_reader.pages[0]
            writer.add_page(_build_replacement_page(original_page, new_page))
        else:
            writer.add_page(reader.pages[i])

    with open(output_pdf_path, 'wb') as f:
        writer.write(f)


def batch_replace_pages(original_pdf_path: str, replacements: dict[int, str], output_pdf_path: str):
    """
    Replaces multiple pages in the original PDF.
    replacements: dict mapping page_number (1-indexed) -> path_to_new_single_page_pdf
    """
    reader = PdfReader(original_pdf_path)
    writer = PdfWriter()

    for i in range(len(reader.pages)):
        page_num = i + 1
        if page_num in replacements:
            original_page = reader.pages[i]
            new_pdf_path = replacements[page_num]
            new_reader = PdfReader(new_pdf_path)
            new_page = new_reader.pages[0]
            writer.add_page(_build_replacement_page(original_page, new_page))
        else:
            writer.add_page(reader.pages[i])

    with open(output_pdf_path, 'wb') as f:
        writer.write(f)


def insert_page(original_pdf_path: str, new_page_pdf_path: str, after_page: int, output_pdf_path: str):
    """
    Inserts a new page into the PDF after the specified page number.
    after_page: 0 to insert at the beginning, or page number (1-indexed) to insert after.
    """
    reader = PdfReader(original_pdf_path)
    writer = PdfWriter()

    reference_page = reader.pages[0]

    new_reader = PdfReader(new_page_pdf_path)
    new_page = new_reader.pages[0]
    prepared_new_page = _build_replacement_page(reference_page, new_page)

    if after_page == 0:
        writer.add_page(prepared_new_page)

    for i in range(len(reader.pages)):
        writer.add_page(reader.pages[i])
        if i + 1 == after_page:
            writer.add_page(prepared_new_page)

    with open(output_pdf_path, 'wb') as f:
        writer.write(f)
