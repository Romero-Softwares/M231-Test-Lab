from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
import re
import unicodedata

from m231pro.core.simulation import Sample, force_from_kn
from m231pro.core.storage import StoredRun


PAGE_WIDTH = 842.0
PAGE_HEIGHT = 595.0
MARGIN = 36.0
ROWS_PER_PAGE = 22

ACTION_LABELS = {
    "inicio": "Início",
    "compressao": "Compressão",
    "tracao": "Tração",
    "descompressao": "Retorno",
    "manter": "Manter posição",
    "aguardar_descompressao": "Espera",
    "limite_seguranca": "Limite de segurança",
}


@dataclass(frozen=True)
class _ReportProfile:
    title: str
    graph_title: str
    x_label: str
    use_time_axis: bool


_REPORT_PROFILES = {
    "compressao": _ReportProfile(
        title="Compressão",
        graph_title="Curva carga × deslocamento",
        x_label="Deslocamento (mm)",
        use_time_axis=False,
    ),
    "tracao_pino": _ReportProfile(
        title="Tração de pino",
        graph_title="Curva carga × tempo",
        x_label="Tempo (s)",
        use_time_axis=True,
    ),
    "tracao_mola": _ReportProfile(
        title="Tração de mola",
        graph_title="Curva carga × tempo",
        x_label="Tempo (s)",
        use_time_axis=True,
    ),
}


def _clean_text(value: object) -> str:
    """Corrige textos antigos salvos como UTF-8 interpretado em cp1252."""
    text = str(value)
    for _ in range(2):
        try:
            repaired = text.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if repaired == text:
            break
        text = repaired
    return text


def _report_profile(specimen_type: str) -> _ReportProfile:
    profile = _REPORT_PROFILES.get(specimen_type)
    if profile is not None:
        return profile
    title = _clean_text(specimen_type.replace("_", " ").strip().title())
    return _ReportProfile(
        title=title or "Ensaio",
        graph_title="Curva carga × deslocamento",
        x_label="Deslocamento (mm)",
        use_time_axis=False,
    )


def format_duration_display(duration_h: float) -> str:
    """Exibe durações curtas em segundos e as demais em horas."""
    duration_h = float(duration_h)
    if 0 <= duration_h < 1:
        return f"{_number(duration_h * 3600.0)} s"
    return f"{_number(duration_h)} h"


def report_filename(run: StoredRun) -> str:
    """Gera um nome de arquivo previsível, legível e seguro."""
    readable_name = _clean_text(run.program.name).strip()
    normalized = unicodedata.normalize("NFKD", readable_name)
    normalized = normalized.encode("ascii", errors="ignore").decode("ascii")
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "-", normalized)
    normalized = normalized.strip("-") or "ensaio"
    return f"relatorio-{run.id:04d}-{normalized}.pdf"


def generate_run_report_pdf(path: str | Path, run: StoredRun) -> None:
    """Gera um PDF autônomo conforme o tipo do ensaio."""
    acquired = [sample for sample in run.samples if sample.acquired]
    document = _PdfDocument()
    pages: list[bytes] = [_summary_page(run, acquired)]
    if acquired:
        for start in range(0, len(acquired), ROWS_PER_PAGE):
            pages.append(_data_page(run, acquired[start : start + ROWS_PER_PAGE], start))
    else:
        pages.append(_data_page(run, [], 0))
    document.write(
        path,
        pages,
        title=f"Relatório do ensaio #{run.id} - {_clean_text(run.program.name)}",
        subject="Relatório de ensaio M231",
    )


def _configuration_lines(run: StoredRun, profile: _ReportProfile) -> list[str]:
    program = run.program
    unit = program.force_unit
    standard = (
        _clean_text(program.standard)
        .replace("referencia", "referência")
        .replace("configuravel", "configurável")
    )
    common = [
        f"Tipo de ensaio: {profile.title}",
        f"Norma/procedimento: {standard}",
        f"Limite de segurança: {_force(program.force_limit_kn, unit)}",
    ]
    if profile.use_time_axis:
        return common + [
            f"Carga-alvo: {_force(program.target_force_kn, unit)}",
            f"Duração: {format_duration_display(program.duration_h)}",
            f"Aquisição temporal: a cada {_number(program.acquisition_interval_s)} s",
            f"Rigidez equivalente: {_number(program.traction_stiffness_kn_mm)} kN/mm",
            f"Retorno automático: {'Sim' if program.auto_return_traction else 'Não'}",
            f"Velocidade de tração: {_number(_traction_speed(program))} mm/min",
        ]
    return common + [
        f"Posição inicial: {_number(program.initial_position_mm)} mm",
        f"Curso permitido: {_number(program.position_min_mm)} a {_number(program.position_max_mm)} mm",
        f"Ciclos: {program.cycles}",
        f"Aquisição: a cada {_number(program.acquisition_interval_mm)} mm",
        f"Espera antes do retorno: {_number(program.wait_before_decompression_s)} s",
        f"Modo: {_clean_text(run.mode)}",
    ]


def _traction_speed(program: object) -> float:
    steps = getattr(program, "steps", [])
    for step in steps:
        if getattr(step, "action", None) == "tracao":
            return float(getattr(step, "speed_mm_min", 0.0))
    return 0.0


def _summary_page(run: StoredRun, samples: list[Sample]) -> bytes:
    canvas = _PdfCanvas()
    program = run.program
    profile = _report_profile(program.specimen_type)
    unit = program.force_unit
    _header(canvas, run, f"Resumo do ensaio - {profile.title}")

    canvas.fill_rect(MARGIN, 449, PAGE_WIDTH - 2 * MARGIN, 56, (0.94, 0.96, 0.98))
    summary = [
        ("Estado", _clean_text(run.status)),
        ("Modo", _clean_text(run.mode)),
        ("Aquisições", str(len(samples))),
        ("Duração", _number(max((item.time_s for item in samples), default=0.0)) + " s"),
        ("Carga máxima", _force(max((abs(item.force_kn) for item in samples), default=0.0), unit)),
        ("Desloc. máximo", _number(max((abs(item.displacement_mm) for item in samples), default=0.0)) + " mm"),
    ]
    column_width = (PAGE_WIDTH - 2 * MARGIN) / len(summary)
    for index, (label, value) in enumerate(summary):
        x = MARGIN + index * column_width + 8
        canvas.text(x, 484, label, 7.5, color=(0.35, 0.39, 0.43))
        canvas.text(x, 465, _clip(value, 26), 10, bold=True, color=(0.08, 0.18, 0.28))

    canvas.text(MARGIN, 428, "Configuração do ensaio", 11, bold=True, color=(0.08, 0.31, 0.50))
    for index, value in enumerate(_configuration_lines(run, profile)):
        x = MARGIN + (index % 3) * 257
        y = 407 - (index // 3) * 18
        canvas.text(x, y, value, 8.5, color=(0.16, 0.18, 0.20))

    canvas.text(
        MARGIN,
        328,
        f"{profile.graph_title} ({unit} × {'s' if profile.use_time_axis else 'mm'})",
        11,
        bold=True,
        color=(0.08, 0.31, 0.50),
    )
    _draw_graph(
        canvas,
        samples,
        unit,
        MARGIN + 48,
        78,
        PAGE_WIDTH - 2 * MARGIN - 64,
        224,
        use_time_axis=profile.use_time_axis,
        x_label=profile.x_label,
    )
    _footer(canvas, 1, 1 + max(1, math.ceil(len(samples) / ROWS_PER_PAGE)))
    return canvas.render()


def _data_page(run: StoredRun, samples: list[Sample], offset: int) -> bytes:
    canvas = _PdfCanvas()
    unit = run.program.force_unit
    profile = _report_profile(run.program.specimen_type)
    acquired_count = len([sample for sample in run.samples if sample.acquired])
    total_pages = 1 + max(1, math.ceil(acquired_count / ROWS_PER_PAGE))
    page_number = 2 + offset // ROWS_PER_PAGE
    _header(canvas, run, f"Tabela de aquisições - {profile.title}")
    columns = [
        ("#", 34),
        ("Tempo (s)", 78),
        (f"Carga ({unit})", 92),
        ("Posição (mm)", 96),
        ("Desloc. (mm)", 103),
        ("Ciclo", 48),
        ("Etapa", 48),
        ("Ação", 235),
    ]
    table_width = sum(width for _, width in columns)
    x = MARGIN
    top = 510.0
    canvas.fill_rect(MARGIN, top, table_width, 23, (0.08, 0.31, 0.50))
    for label, width in columns:
        canvas.text(x + 5, top + 8, label, 7.5, bold=True, color=(1, 1, 1))
        x += width

    if not samples:
        canvas.text(MARGIN + 8, top - 28, "Nenhuma aquisição registrada.", 9)
    for row_index, sample in enumerate(samples):
        y = top - (row_index + 1) * 21
        if row_index % 2:
            canvas.fill_rect(MARGIN, y, table_width, 21, (0.96, 0.97, 0.98))
        values = [
            str(offset + row_index + 1),
            _number(sample.time_s),
            _number(force_from_kn(sample.force_kn, unit)),
            _number(sample.position_mm),
            _number(sample.displacement_mm),
            str(sample.cycle),
            str(sample.step),
            ACTION_LABELS.get(sample.action, _clean_text(sample.action)),
        ]
        x = MARGIN
        for value, (_, width) in zip(values, columns):
            canvas.text(x + 5, y + 7, _clip(value, max(4, int(width / 5.2))), 7.5)
            x += width
        canvas.line(MARGIN, y, MARGIN + table_width, y, color=(0.84, 0.86, 0.88), width=0.35)
    _footer(canvas, page_number, total_pages)
    return canvas.render()


def _header(canvas: "_PdfCanvas", run: StoredRun, section: str) -> None:
    canvas.text(MARGIN, 558, "M231 TEST LAB", 18, bold=True, color=(0.04, 0.25, 0.42))
    canvas.text(MARGIN, 541, section, 9, color=(0.35, 0.39, 0.43))
    canvas.text(PAGE_WIDTH - MARGIN, 558, f"RELATÓRIO #{run.id}", 11, bold=True, align="right", color=(0.04, 0.25, 0.42))
    canvas.text(PAGE_WIDTH - MARGIN, 541, _format_date(run.started_at), 8, align="right", color=(0.35, 0.39, 0.43))
    canvas.line(MARGIN, 532, PAGE_WIDTH - MARGIN, 532, color=(0.08, 0.31, 0.50), width=1.2)
    canvas.text(MARGIN, 518, _clip(run.program.name, 85), 10.5, bold=True)


def _footer(canvas: "_PdfCanvas", page: int, total: int) -> None:
    canvas.line(MARGIN, 31, PAGE_WIDTH - MARGIN, 31, color=(0.75, 0.77, 0.79), width=0.5)
    canvas.text(MARGIN, 18, "Gerado pelo M231 Test Lab", 7, color=(0.45, 0.47, 0.49))
    canvas.text(PAGE_WIDTH - MARGIN, 18, f"Página {page} de {total}", 7, align="right", color=(0.45, 0.47, 0.49))


def _draw_graph(
    canvas: "_PdfCanvas",
    samples: list[Sample],
    unit: str,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    use_time_axis: bool = False,
    x_label: str | None = None,
) -> None:
    canvas.stroke_rect(x, y, width, height, color=(0.55, 0.58, 0.60), width=0.7)
    if not samples:
        canvas.text(x + width / 2, y + height / 2, "Sem aquisições", 9, align="center", color=(0.45, 0.47, 0.49))
        return
    values_x = [sample.time_s for sample in samples] if use_time_axis else [sample.displacement_mm for sample in samples]
    values_y = [force_from_kn(sample.force_kn, unit) for sample in samples]
    min_x, max_x = min(values_x), max(values_x)
    min_y, max_y = min(values_y), max(values_y)
    if abs(max_x - min_x) < 1e-12:
        min_x -= 0.5
        max_x += 0.5
    if abs(max_y - min_y) < 1e-12:
        padding = max(abs(max_y) * 0.1, 1.0)
        min_y -= padding
        max_y += padding
    else:
        padding = (max_y - min_y) * 0.08
        min_y -= padding
        max_y += padding

    for index in range(6):
        fraction = index / 5
        grid_x = x + fraction * width
        grid_y = y + fraction * height
        canvas.line(grid_x, y, grid_x, y + height, color=(0.88, 0.89, 0.90), width=0.35)
        canvas.line(x, grid_y, x + width, grid_y, color=(0.88, 0.89, 0.90), width=0.35)
        canvas.text(grid_x, y - 13, _number(min_x + fraction * (max_x - min_x)), 6.5, align="center", color=(0.35, 0.37, 0.39))
        canvas.text(x - 6, grid_y - 2, _number(min_y + fraction * (max_y - min_y)), 6.5, align="right", color=(0.35, 0.37, 0.39))

    points = [
        (
            x + (value_x - min_x) / (max_x - min_x) * width,
            y + (force_from_kn(sample.force_kn, unit) - min_y) / (max_y - min_y) * height,
        )
        for sample, value_x in zip(samples, values_x)
    ]
    canvas.polyline(points, color=(0.02, 0.42, 0.72), width=1.3)
    canvas.text(x + width / 2, y - 29, x_label or ("Tempo (s)" if use_time_axis else "Deslocamento (mm)"), 7.5, align="center")
    canvas.text(x, y + height + 7, f"Carga ({unit})", 7.5)


def _format_date(value: str) -> str:
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.strftime("%d/%m/%Y %H:%M:%S UTC")
    except ValueError:
        return _clean_text(value)


def _number(value: float) -> str:
    rounded = round(float(value), 4)
    if abs(rounded) < 1e-12:
        rounded = 0.0
    return f"{rounded:.4f}".rstrip("0").rstrip(".").replace(".", ",")


def _force(value_kn: float, unit: str) -> str:
    return f"{_number(force_from_kn(value_kn, unit))} {unit}"


def _clip(value: object, length: int) -> str:
    text = _clean_text(value)
    return text if len(text) <= length else text[: max(1, length - 1)] + "…"


class _PdfCanvas:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def text(
        self,
        x: float,
        y: float,
        value: object,
        size: float,
        *,
        bold: bool = False,
        align: str = "left",
        color: tuple[float, float, float] = (0, 0, 0),
    ) -> None:
        text = _clean_text(value)
        approximate_width = len(text) * size * 0.49
        if align == "right":
            x -= approximate_width
        elif align == "center":
            x -= approximate_width / 2
        encoded = text.encode("cp1252", errors="replace").hex().upper()
        font = "F2" if bold else "F1"
        self.commands.append(
            f"BT /{font} {size:.2f} Tf {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg "
            f"1 0 0 1 {x:.2f} {y:.2f} Tm <{encoded}> Tj ET"
        )

    def line(self, x1: float, y1: float, x2: float, y2: float, *, color: tuple[float, float, float], width: float) -> None:
        self.commands.append(
            f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG {width:.2f} w "
            f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S"
        )

    def polyline(self, points: list[tuple[float, float]], *, color: tuple[float, float, float], width: float) -> None:
        if not points:
            return
        path = [f"{points[0][0]:.2f} {points[0][1]:.2f} m"]
        path.extend(f"{x:.2f} {y:.2f} l" for x, y in points[1:])
        self.commands.append(
            f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG {width:.2f} w "
            + " ".join(path)
            + " S"
        )

    def fill_rect(self, x: float, y: float, width: float, height: float, color: tuple[float, float, float]) -> None:
        self.commands.append(
            f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg "
            f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re f"
        )

    def stroke_rect(self, x: float, y: float, rect_width: float, rect_height: float, *, color: tuple[float, float, float], width: float) -> None:
        self.commands.append(
            f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG {width:.2f} w "
            f"{x:.2f} {y:.2f} {rect_width:.2f} {rect_height:.2f} re S"
        )

    def render(self) -> bytes:
        return ("\n".join(self.commands) + "\n").encode("ascii")


class _PdfDocument:
    def __init__(self) -> None:
        self.objects: list[bytes | None] = [None]

    def _reserve(self) -> int:
        self.objects.append(None)
        return len(self.objects) - 1

    def _add(self, value: bytes) -> int:
        self.objects.append(value)
        return len(self.objects) - 1

    def write(self, path: str | Path, pages: list[bytes], title: str, subject: str) -> None:
        font_regular = self._add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        font_bold = self._add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        pages_id = self._reserve()
        page_ids: list[int] = []
        for page in pages:
            content_id = self._add(
                f"<< /Length {len(page)} >>\nstream\n".encode("ascii") + page + b"endstream"
            )
            page_id = self._add(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_WIDTH:.0f} {PAGE_HEIGHT:.0f}] "
                    f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >> "
                    f"/Contents {content_id} 0 R >>"
                ).encode("ascii")
            )
            page_ids.append(page_id)
        kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
        self.objects[pages_id] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")
        catalog_id = self._add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"))
        info_id = self._add(
            f"<< /Title <{_pdf_hex(title)}> /Subject <{_pdf_hex(subject)}> /Creator <{_pdf_hex('M231 Test Lab')}> >>".encode("ascii")
        )

        output = bytearray(b"%PDF-1.4\n%\xE2\xE3\xCF\xD3\n")
        offsets = [0]
        for object_id, value in enumerate(self.objects[1:], start=1):
            if value is None:
                raise RuntimeError(f"Objeto PDF {object_id} não foi definido.")
            offsets.append(len(output))
            output.extend(f"{object_id} 0 obj\n".encode("ascii"))
            output.extend(value)
            output.extend(b"\nendobj\n")
        xref = len(output)
        output.extend(f"xref\n0 {len(self.objects)}\n".encode("ascii"))
        output.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        output.extend(
            (
                f"trailer\n<< /Size {len(self.objects)} /Root {catalog_id} 0 R /Info {info_id} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n"
            ).encode("ascii")
        )
        Path(path).write_bytes(output)


def _pdf_hex(value: str) -> str:
    return _clean_text(value).encode("cp1252", errors="replace").hex().upper()
