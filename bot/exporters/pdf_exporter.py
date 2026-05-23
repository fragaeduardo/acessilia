from pathlib import Path
from typing import Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    ListFlowable, ListItem,
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas

from bot.utils.logger import logger


class _OutlineDocTemplate(SimpleDocTemplate):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._outline_data = []
        self._outline_counter = 0
        self._prev_level = 0
        self._outline_built = False

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph):
            name = flowable.style.name
            level = {"AccessibleH1": 1, "AccessibleH2": 2, "AccessibleH3": 3}.get(name)
            if level is not None:
                text = flowable.getPlainText()
                adj_level = level - 1
                if adj_level > self._prev_level + 1:
                    adj_level = self._prev_level + 1
                key = f"bm{self._outline_counter}"
                self._outline_counter += 1
                self.canv.bookmarkPage(key)
                self._outline_data.append((adj_level, text, key))
                self._prev_level = adj_level
        super().afterFlowable(flowable)

    def handle_pageEnd(self):
        if not self._outline_built and self._outline_data:
            current_level = -1
            for level, text, key in self._outline_data:
                if level > current_level + 1:
                    level = current_level + 1
                self.canv.addOutlineEntry(text, key, level, closed=False)
                current_level = level
            self._outline_built = True
        super().handle_pageEnd()


from bot.utils.text_processor import parse_markdown_and_descriptions
import re

def export_pdf(
    text: str,
    output_path: Path,
    title: str = "Documento Acessível",
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Coleta cabeçalhos para o Índice
    structured_content = parse_markdown_and_descriptions(text)
    headings = []
    for entry_type, content in structured_content:
        if entry_type == 'h1': headings.append((1, content))
        elif entry_type == 'h2': headings.append((2, content))
        elif entry_type == 'h3': headings.append((3, content))

    def add_page_number(canvas_obj: canvas.Canvas, doc):
        canvas_obj.saveState()
        canvas_obj.setFont("Helvetica", 9)
        page_num = canvas_obj.getPageNumber()
        canvas_obj.drawCentredString(
            A4[0] / 2, 12 * mm, f"- {page_num} -"
        )
        canvas_obj.restoreState()

    doc = _OutlineDocTemplate(
        str(output_path),
        pagesize=A4,
        topMargin=20 * mm,
        bottomMargin=25 * mm,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        title=title,
        language="pt-BR",
        author="Bot Acess",
    )

    styles = getSampleStyleSheet()

    # Estilos customizados (mantendo os anteriores e adicionando novos)
    title_style = ParagraphStyle("AccessibleTitle", parent=styles["Title"], fontSize=18, alignment=TA_CENTER)
    heading1 = ParagraphStyle("AccessibleH1", parent=styles["Heading1"], fontSize=16, spaceBefore=12, textColor=HexColor("#1a1a2e"))
    heading2 = ParagraphStyle("AccessibleH2", parent=styles["Heading2"], fontSize=14, spaceBefore=10, textColor=HexColor("#16213e"))
    heading3 = ParagraphStyle("AccessibleH3", parent=styles["Heading3"], fontSize=12, spaceBefore=8, textColor=HexColor("#0f3460"))
    body = ParagraphStyle("AccessibleBody", parent=styles["Normal"], fontSize=11, leading=15, spaceAfter=6)
    
    desc_style = ParagraphStyle(
        "AccessibleDesc",
        parent=body,
        fontSize=10,
        leftIndent=10 * mm,
        borderPadding=5,
        backColor=HexColor("#f8f9fa"),
        textColor=HexColor("#495057"),
        italic=True
    )

    elements = []
    elements.append(Spacer(1, 30 * mm))
    elements.append(Paragraph(title, title_style))
    elements.append(Spacer(1, 10 * mm))

    # Índice (omitido para brevidade no replace, mas mantido na lógica)
    if headings:
        elements.append(Paragraph("Índice", styles["Heading1"]))
        for level, h_text in headings:
            elements.append(Paragraph(f"{'  '*(level-1)}{h_text}", body))
        elements.append(PageBreak())

    def md_to_xml(txt):
        """Converte Markdown para XML do ReportLab com aninhamento correto (stack-based)."""
        # Remove lixo de conversação
        txt = re.sub(r"\[.*?Deseja que eu corrija.*?\]", "", txt, flags=re.IGNORECASE)
        txt = re.sub(r"Deseja que eu corrija.*?\?", "", txt, flags=re.IGNORECASE)

        # Fila de tokens Markdown
        # Regex para achar tags de bold/italic
        parts = re.split(r"(\*\*\*|\*\*|\*)", txt)
        
        stack = []
        result = []
        
        for part in parts:
            if part == '***':
                if 'i' in stack and 'b' in stack: # Fecha ambos
                    result.append("</i></b>")
                    stack.remove('i'); stack.remove('b')
                else: # Abre ambos
                    result.append("<b><i>")
                    stack.append('b'); stack.append('i')
            elif part == '**':
                if 'b' in stack:
                    result.append("</b>")
                    stack.remove('b')
                else:
                    result.append("<b>")
                    stack.append('b')
            elif part == '*':
                if 'i' in stack:
                    result.append("</i>")
                    stack.remove('i')
                else:
                    result.append("<i>")
                    stack.append('i')
            else:
                result.append(part)
        
        # Fecha o que sobrou na pilha (ordem inversa)
        for tag in reversed(stack):
            result.append(f"</{tag}>")
            
        final_txt = "".join(result)
        return final_txt.replace("<br>", "<br/>")

    current_list_items = []
    list_type = None

    def flush_list(items, ltype):
        if not items: return None
        bullet = "bullet" if ltype == "bullet" else "1"
        return ListFlowable(
            [ListItem(Paragraph(md_to_xml(item), body)) for item in items],
            bulletType=bullet,
            leftIndent=20,
            spaceBefore=6
        )

    for entry_type, content in structured_content:
        # Se era lista e agora não é, descarrega a lista
        if list_type and entry_type not in ('bullet', 'number'):
            elements.append(flush_list(current_list_items, list_type))
            current_list_items = []
            list_type = None

        if entry_type == 'h1': elements.append(Paragraph(md_to_xml(content), heading1))
        elif entry_type == 'h2': elements.append(Paragraph(md_to_xml(content), heading2))
        elif entry_type == 'h3': elements.append(Paragraph(md_to_xml(content), heading3))
        elif entry_type in ('bullet', 'number'):
            new_type = "bullet" if entry_type == 'bullet' else "number"
            if list_type and list_type != new_type:
                elements.append(flush_list(current_list_items, list_type))
                current_list_items = []
            list_type = new_type
            current_list_items.append(content)
        elif entry_type == 'description':
            # No PDF, usamos um parágrafo especial que simula o Alt-Text estruturado
            elements.append(Paragraph("<b>[INÍCIO DA AUDIODESCRIÇÃO]</b>", desc_style))
            elements.append(Paragraph(md_to_xml(content), desc_style))
            elements.append(Paragraph("<b>[FIM DA AUDIODESCRIÇÃO]</b>", desc_style))
        else:
            elements.append(Paragraph(md_to_xml(content), body))

    if current_list_items:
        elements.append(flush_list(current_list_items, list_type))

    doc.build(elements, onFirstPage=add_page_number, onLaterPages=add_page_number)
    return output_path
    logger.debug("PDF exportado com bookmarks e numeracao: {}", output_path)
    return output_path
