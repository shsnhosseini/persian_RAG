# Extract text from Word, Excel and PDF, apply normalization, and chunk while preserving metadata (crucial for citations).
import os
import pandas as pd
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from utils import normalize_persian_text
from langchain_core.documents import Document

try:
    from docx import Document as DocxDocument
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph
    from docx.table import Table
    _HAS_PYTHON_DOCX = True
except ImportError:
    _HAS_PYTHON_DOCX = False

# ---------------------------------------------------------------------------
# DOCX page handling
# ---------------------------------------------------------------------------
# Word documents do NOT have fixed pages like PDFs; a rendered page number depends
# on page size, margins, font, and printer. Assuming Microsoft Word is installed on
# this machine, we use Word's own COM automation to get REAL rendered page numbers.
#
#   load_docx_with_pages(path):
#       1. Word COM automation (primary): open .docx in invisible Word, convert to
#          PDF via SaveAs2 (wdFormatPDF = 17), read exact pages with PyPDFLoader.
#          PDF pages are fixed, so this gives 100% accurate rendered page numbers.
#          Requires pywin32 + Microsoft Word (Windows).
#       2. python-docx fallback (if Word COM unavailable/fails): honour explicit
#          Word page breaks; documents without explicit breaks are split into
#          *estimated* pages (approximate, not exact rendered pages).
# ---------------------------------------------------------------------------

try:
    import win32com.client
    _HAS_PYWIN32 = True
except ImportError:
    _HAS_PYWIN32 = False

# Conservative estimate of characters that fit on one rendered page (11 pt, ~1 inch
# margins). Used only by the python-docx fallback when Word COM is unavailable.
_ESTIMATED_CHARS_PER_PAGE = 3000


def _load_docx_via_word(path: str):
    """Render a .docx through Microsoft Word (COM) and return real 1-based page Documents.

    Converts .docx to .pdf using Word's own SaveAs2 (wdFormatPDF = 17), then reads
    exact page numbers from the PDF with PyPDFLoader. PDF pages are fixed, so this
    gives 100% accurate rendered page numbers — no paragraph-level guessing.
    Requires Microsoft Word installed (Windows) and pywin32. Retries up to 3 times
    because Word may be busy on the first attempt.
    """
    if not _HAS_PYWIN32:
        return None
    import win32com.client
    import time

    last_error = None
    for attempt in range(3):
        w = None
        doc = None
        pdf_path = None
        try:
            w = win32com.client.Dispatch("Word.Application")
            w.Visible = False
            w.DisplayAlerts = False
            doc = w.Documents.Open(path)

            # Save as PDF using Word's own engine (wdFormatPDF = 17)
            pdf_path = path.rsplit(".", 1)[0] + ".pdf"
            # Remove existing temp pdf if any
            try:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)
            except OSError:
                pass
            doc.SaveAs2(pdf_path, 17)  # 17 = wdFormatPDF
            doc.Close(False)
            doc = None

            # Give Word a moment to release the file
            time.sleep(0.5)

            # Verify the PDF was created and is non-empty
            if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
                last_error = "PDF not created or empty"
                continue

            # Read exact pages from the generated PDF
            loader = PyPDFLoader(pdf_path)
            pages = loader.load()
            for doc_item in pages:
                page = doc_item.metadata.get("page", 0)
                if isinstance(page, bool) or page is None:
                    doc_item.metadata["page"] = "1"
                elif isinstance(page, (int, float)):
                    doc_item.metadata["page"] = str(int(page) + 1)  # 0-based -> 1-based
                else:
                    doc_item.metadata["page"] = str(page).strip() or "1"
            if pages:
                return pages
            return None
        except Exception as e:
            last_error = str(e)
            if attempt < 2:
                time.sleep(1.0)
                continue
            return None
        finally:
            if doc is not None:
                try:
                    doc.Close(False)
                except Exception:
                    pass
            if w is not None:
                try:
                    w.Quit()
                except Exception:
                    pass
            # Clean up temp pdf
            if pdf_path:
                try:
                    if os.path.exists(pdf_path):
                        os.remove(pdf_path)
                except OSError:
                    pass
    return None


def _flush_page(current_text, page_num):
    text = "\n".join(t for t in current_text if t)
    if text.strip():
        return (
            Document(page_content=text, metadata={"page": str(page_num)}),
            page_num + 1,
        )
    return None, page_num


def _has_explicit_page_break(body) -> bool:
    for child in body.iterchildren():
        if child.tag != qn('w:p'):
            continue
        for run in child.findall(qn('w:r')):
            for br in run.findall(qn('w:br')):
                if br.get(qn('w:type')) == 'page':
                    return True
    return False


def _split_into_estimated_pages(pages):
    """Re-split single-page Documents into estimated multi-page Documents.

    Splits at paragraph boundaries so paragraphs are not cut mid-way.
    """
    result = []
    page_num = 1
    for doc in pages:
        text = doc.page_content or ""
        paragraphs = [p for p in text.split("\n") if p.strip()]
        if not paragraphs:
            result.append(
                Document(page_content="", metadata={"page": str(page_num)})
            )
            page_num += 1
            continue
        current = []
        current_len = 0
        for para in paragraphs:
            add_len = len(para) + (1 if current else 0)
            if current and current_len + add_len > _ESTIMATED_CHARS_PER_PAGE:
                result.append(
                    Document(
                        page_content="\n".join(current),
                        metadata={"page": str(page_num)},
                    )
                )
                page_num += 1
                current = [para]
                current_len = len(para)
            else:
                current.append(para)
                current_len += add_len
        if current:
            result.append(
                Document(
                    page_content="\n".join(current),
                    metadata={"page": str(page_num)},
                )
            )
            page_num += 1
    return result


def _load_docx_fallback(path: str):
    """python-docx fallback: explicit page breaks only; whole doc = page 1 if none."""
    doc = DocxDocument(path)
    pages = []
    current_text = []
    page_num = 1

    # Iterate paragraphs and tables in document order via the body XML
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag
        if tag == qn('w:p'):
            para = Paragraph(child, doc)
            # Check runs in this paragraph for an explicit page break
            has_page_break = False
            for run in para._element.findall(qn('w:r')):
                for br in run.findall(qn('w:br')):
                    if br.get(qn('w:type')) == 'page':
                        has_page_break = True
                        break
                if has_page_break:
                    break

            if has_page_break:
                # End current page after this paragraph; break pushes following
                # content to the next page (matches Word's page-break semantics)
                current_text.append(para.text)
                page_doc, page_num = _flush_page(current_text, page_num)
                if page_doc:
                    pages.append(page_doc)
                current_text = []
            else:
                current_text.append(para.text)
        elif tag == qn('w:tbl'):
            table = Table(child, doc)
            for row in table.rows:
                row_text = " | ".join(cell.text for cell in row.cells)
                current_text.append(row_text)

    page_doc, _ = _flush_page(current_text, page_num)
    if page_doc:
        pages.append(page_doc)

    # If no explicit page breaks were used, split the collected content into
    # estimated pages so a long document is not collapsed into a single page 1.
    if not _has_explicit_page_break(body):
        pages = _split_into_estimated_pages(pages)

    if not pages:
        pages.append(Document(page_content="", metadata={"page": "1"}))
    return pages


def load_docx_with_pages(path: str):
    """Load a .docx file and split into pages with 1-based page numbers.

    Priority order:
    1. Word COM automation (primary, Windows only): convert .docx to PDF via
       Word's SaveAs2, then read exact pages with PyPDFLoader. Gives 100% accurate
       rendered page numbers for the convertible portion. Retries up to 3 times.
    2. python-docx fallback (if Word COM unavailable/fails): honour explicit Word
       page breaks (<w:br w:type="page">) inserted manually in Word. Documents
       without explicit breaks are split into *estimated* pages (approximate, not
       exact rendered pages) so multi-page documents are not collapsed into page 1.

    Returns a list of Document objects with 1-based page numbers in metadata.
    """
    # Try Word COM automation first (primary method)
    pages = _load_docx_via_word(path)
    if pages is not None:
        # Word COM succeeded - return what we got (may be accurate for pages 1-4,
        # beyond that depends on Word's PDF conversion quality)
        return pages

    # Word COM failed entirely (can't open, convert, or read PDF) - fall back
    # to python-docx explicit page breaks + estimated pages
    return _load_docx_fallback(path)


def load_excel_documents(path: str, original_name: str | None = None):
    """Loads Excel files and returns Document objects per non-empty row.

    Each non-empty row in every sheet is converted to a Document whose
    page_content is a list of "column: value" pairs (non-empty cells only).
    The page metadata is set to "SheetName | R{row_number}" (1-based row number)
    so citations can anchor answers to a specific location in the workbook,
    mirroring the PDF/DOCX page-anchoring scheme. The source_name is set to the
    user's original uploaded file name (via original_name) or the file path's
    basename.

    The ExcelFile is opened as a context manager so the file handle is always
    closed after reading, preventing PermissionError on Windows temp files.
    """
    docs = []
    with pd.ExcelFile(path) as xlsx:
        for sheet_name in xlsx.sheet_names:
            df = xlsx.parse(sheet_name)
            columns = [str(c) for c in df.columns]
            for row_idx, row in df.iterrows():
                row_number = row_idx + 1
                cells = []
                for col in columns:
                    val = row[col]
                    if pd.notna(val) and str(val).strip() != "":
                        cells.append(f"{col}: {val}")
                if cells:
                    content = "\n".join(cells)
                    docs.append(
                        Document(
                            page_content=content,
                            metadata={
                                "source_name": original_name or os.path.basename(path),
                                "page": f"{sheet_name} | R{row_number}",
                                "sheet_name": sheet_name,
                                "row_number": row_number,
                            },
                        )
                    )
    return docs


def load_documents(file_paths: list[str], original_names: dict | None = None):
    """Loads PDF and Word documents.

    original_names maps a temp file path to the user's original uploaded file name
    so citations show the real file name instead of a temp path.
    """
    docs = []
    for path in file_paths:
        ext = os.path.splitext(path)[1].lower()
        if ext == '.pdf':
            loader = PyPDFLoader(path)
            loaded_docs = loader.load()
        elif ext in ['.docx', '.doc']:
            if _HAS_PYTHON_DOCX:
                loaded_docs = load_docx_with_pages(path)
            else:
                from langchain_community.document_loaders import Docx2txtLoader
                loader = Docx2txtLoader(path)
                loaded_docs = loader.load()
                for doc in loaded_docs:
                    doc.metadata['page'] = '1'
        elif ext in ['.xlsx', '.xls']:
            original_name = original_names.get(path) if original_names else None
            loaded_docs = load_excel_documents(path, original_name)
        else:
            continue
        for doc in loaded_docs:
            if original_names and path in original_names:
                doc.metadata['source_name'] = original_names[path]
            else:
                doc.metadata['source_name'] = os.path.basename(
                    doc.metadata.get('source', 'Unknown')
                )
        docs.extend(loaded_docs)
    return docs

def process_and_chunk_documents(docs):
    """Normalizes text and splits into chunks."""
    # Normalize text in-place and ensure consistent metadata types
    for doc in docs:
        doc.page_content = normalize_persian_text(doc.page_content)
        # Ensure source filename is in metadata (keep original name if already set)
        doc.metadata['source_name'] = doc.metadata.get(
            'source_name'
        ) or os.path.basename(doc.metadata.get('source', 'Unknown'))
        # Normalize page to a consistent string type to avoid ES mapping conflicts
        # (PyPDFLoader gives int page index = 0-based; convert to 1-based for users)
        page = doc.metadata.get('page', 'N/A')
        if isinstance(page, bool) or page is None:
            doc.metadata['page'] = 'N/A'
        elif isinstance(page, (int, float)):
            doc.metadata['page'] = str(int(page) + 1)
        else:
            doc.metadata['page'] = str(page).strip() or 'N/A'

    # Drop empty chunks so they don't fail indexing
    docs = [d for d in docs if d.page_content and d.page_content.strip()]

    # Chunking. Separators are adjusted slightly for Persian/English mixed text
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=150,
        separators=["\n\n", "\n", ". ", " ", "،", ""]
    )

    chunks = text_splitter.split_documents(docs)
    # Drop any empty chunks produced by the splitter
    chunks = [c for c in chunks if c.page_content and c.page_content.strip()]
    return chunks
