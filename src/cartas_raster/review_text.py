"""Portuguese pull-request text. One chart per pull request, no shared-file conflicts."""

from __future__ import annotations

from cartas_raster.catalog import chart_fragment
from cartas_raster.pipeline import edition_label


MARKER = "<!-- cartas-raster chart_id={chart_id} sha256={sha256} -->"


def proposal_document(proposed: dict, previous: dict | None) -> dict:
    return {
        "chart_id": proposed["chart_id"],
        "title": proposed["title"],
        "source_url": proposed["source_url"],
        "page_url": proposed.get("page_url"),
        "previous": _summary(previous) if previous else None,
        "proposed": proposed,
    }


def pull_request_title(proposed: dict) -> str:
    return f"Carta {proposed['chart_id']} — {proposed['title']}"


def pull_request_body(proposed: dict, previous: dict | None, repository: str) -> str:
    kind = "Atualização" if previous else "Carta nova"
    lines = [
        f"## {kind}: carta {proposed['chart_id']}",
        "",
        f"- **Número:** {proposed['chart_id']}",
        f"- **Título:** {proposed['title']}",
        f"- **Edição anterior:** {edition_label(previous)}",
        f"- **Edição nova:** {edition_label(proposed)}",
        f"- **Tamanho anterior:** {_size(previous)}",
        f"- **Tamanho novo:** {_size(proposed)}",
        f"- **SHA-256 novo:** `{proposed['sha256']}`",
        f"- **URL de origem (ZIP da DHN):** {proposed['source_url']}",
    ]
    if proposed.get("page_url"):
        lines.append(f"- **Página no CHM:** {proposed['page_url']}")
    lines.extend(
        [
            "",
            "Mesclar este PR publica a carta: o workflow baixa o ZIP de novo, confere o SHA-256, "
            "envia o arquivo para a Release `cartas` e regenera o catálogo do Chart Downloader.",
            "Fechar este PR sem mesclar rejeita esta edição. Ela não volta a ser proposta "
            "enquanto o arquivo na DHN não mudar.",
            "",
            "### Fragmento que entra no catálogo (`RncProductCatalog`)",
            "",
            "```xml",
            chart_fragment(proposed, repository),
            "```",
            "",
            MARKER.format(chart_id=proposed["chart_id"], sha256=proposed["sha256"]),
        ]
    )
    return "\n".join(lines) + "\n"


def parse_marker(text: str) -> tuple[str, str] | None:
    marker = "cartas-raster chart_id="
    index = text.find(marker)
    if index == -1:
        return None
    rest = text[index + len(marker) :]
    chart_id, _, tail = rest.partition(" sha256=")
    sha, _, _ = tail.partition(" ")
    sha = sha.strip().rstrip("-").rstrip(">")
    chart_id = chart_id.strip()
    if not chart_id or not sha:
        return None
    return chart_id, sha


def _summary(entry: dict) -> dict:
    keys = (
        "chart_id",
        "title",
        "source_edition",
        "raster_edition",
        "source_date",
        "ntm_edition",
        "ntm_date",
        "content_length",
        "sha256",
        "zip_datetime",
        "source_url",
    )
    return {key: entry.get(key) for key in keys}


def _size(entry: dict | None) -> str:
    if not entry or entry.get("content_length") is None:
        return "—"
    return f"{int(entry['content_length'])} bytes"
