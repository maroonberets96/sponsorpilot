import os
import markdown
from fpdf import FPDF
from fpdf.fonts import FontFace
from logger import get_logger
logger = get_logger()


class CustomPDF(FPDF):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_margins(20, 20, 20)
        self.set_auto_page_break(auto=True, margin=20)
        
    def header(self):
        # Subtle top padding
        self.ln(5)

    def footer(self):
        # Clean page numbers
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

def convert_markdown_to_pdf(md_text, output_pdf_path):
    """Converts a Markdown string to a professionally formatted PDF file."""
    # Strip LLM markdown wrappers
    md_text = md_text.replace("```markdown\n", "").replace("```markdown", "").replace("```", "").strip()
    
    # Replace common unicode characters that Helvetica doesn't support
    replacements = {
        '“': '"', '”': '"', '‘': "'", '’': "'", 
        '—': '-', '–': '-', '•': '-', '…': '...',
        ' ': ' '
    }
    for k, v in replacements.items():
        md_text = md_text.replace(k, v)
        
    # Ignore any remaining non-latin-1 characters
    md_text = md_text.encode('latin-1', 'ignore').decode('latin-1')
    
    # Convert Markdown to HTML
    html_content = markdown.markdown(md_text, extensions=['tables'])
    
    # Force center alignment for Name and Contact Info (First h1 and p)
    html_content = html_content.replace("<h1>", '<h1 align="center">', 1)
    html_content = html_content.replace("<p>", '<p align="center">', 1)
    
    # Initialize PDF document
    pdf = CustomPDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    
    # Set default typography and colors
    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(31, 41, 55) # Off-black color (#1f2937)
    pdf.set_fill_color(31, 41, 55) # Ensure bullets are drawn dark gray
    pdf.set_draw_color(31, 41, 55)
    
    # Define professional, clean colors and sizes for headings to override FPDF2's default red headings
    tag_styles = {
        "h1": FontFace(color="#111827", size_pt=18),
        "h2": FontFace(color="#1f2937", size_pt=13),
        "h3": FontFace(color="#374151", size_pt=11),
        "p": FontFace(color="#1f2937", size_pt=9.5),
        "li": FontFace(color="#1f2937", size_pt=9.5),
        "ul": FontFace(color="#1f2937", size_pt=9.5),
        "center": FontFace(color="#111827", size_pt=10),
    }
    
    try:
        # FPDF2 built-in HTML parsing and rendering with custom tag styles
        pdf.write_html(html_content, tag_styles=tag_styles)
        pdf.output(output_pdf_path)
        logger.info(f"Successfully generated PDF: {output_pdf_path}")
    except Exception as e:
        logger.error(f"Failed to generate PDF for {output_pdf_path}: {e}")
        raise e
