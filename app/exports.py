"""CSV, HTML, and ZIP support exports without credential material."""

from __future__ import annotations

import csv
import html
import io
import json
import secrets
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app import __version__
from app.config import AppPaths
from app.db import ConnectionRepository, RunRepository
from app.run_logging import RunLogStore
from app.settings_store import SettingsStore
from app.statuses import (
    RUN_ERROR_LABELS,
    SUCCESSFUL_RUN_RESULTS,
    enrich_file,
    enrich_run,
)


RUN_FIELDS = (
    "id",
    "connection_id",
    "connection_name",
    "trigger",
    "window_start_utc",
    "window_end_utc",
    "started_at",
    "finished_at",
    "status",
    "result_status",
    "status_label",
    "files_found",
    "files_downloaded",
    "files_skipped",
    "files_failed",
    "bytes_downloaded",
    "error_type",
    "error_msg",
)
FILE_FIELDS = (
    "id",
    "run_id",
    "connection_id",
    "connection_name",
    "remote_path",
    "local_path",
    "size_bytes",
    "bytes_done",
    "mtime_utc",
    "sha256",
    "status",
    "status_label",
    "attempts",
    "error_type",
    "error_msg",
    "started_at",
    "finished_at",
    "duration_s",
    "average_bps",
    "run_started_at",
)


class ExportService:
    """Build operator exports from persisted audit state."""

    def __init__(
        self,
        *,
        paths: AppPaths,
        runs: RunRepository,
        connections: ConnectionRepository,
        settings: SettingsStore,
        run_logs: RunLogStore,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.paths = paths
        self.runs = runs
        self.connections = connections
        self.settings = settings
        self.run_logs = run_logs
        self.now = now

    def runs_csv(self, *, days: int = 30) -> str:
        return _csv_text(
            self._runs(days=days),
            RUN_FIELDS,
        )

    def files_csv(
        self,
        *,
        run_id: int | None = None,
        connection_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        status: str | None = None,
    ) -> str:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.runs.list_files(
                run_id=run_id,
                connection_id=connection_id,
                status=status,
                date_from=date_from,
                date_to=date_to,
                limit=1000,
                offset=offset,
            )
            rows.extend(enrich_file(row) for row in page)
            if len(page) < 1000:
                break
            offset += len(page)
        return _csv_text(rows, FILE_FIELDS)

    def safe_configuration(self) -> dict[str, Any]:
        return {
            "app": "Recolecta",
            "version": __version__,
            "exported_at": self.now().astimezone(timezone.utc).isoformat(),
            "note": (
                "Los secretos no se exportan; deberán reingresarse al restaurar."
            ),
            "settings": {
                key: value
                for key, value in self.settings.all().items()
                if not _sensitive_key(key)
            },
            "connections": [
                connection.to_public_dict()
                for connection in self.connections.list()
            ],
        }

    def html_report(
        self,
        *,
        days: int = 30,
        client: str | None = None,
    ) -> str:
        rows = self._runs(days=days)
        if client:
            connection_ids = {
                item.id
                for item in self.connections.list()
                if item.client.casefold() == client.casefold()
            }
            rows = [
                row for row in rows if row["connection_id"] in connection_ids
            ]
        total = len(rows)
        successful = sum(
            row["result_status"] in SUCCESSFUL_RUN_RESULTS for row in rows
        )
        files = sum(int(row["files_downloaded"] or 0) for row in rows)
        volume = sum(int(row["bytes_downloaded"] or 0) for row in rows)
        errors = Counter(
            RUN_ERROR_LABELS.get(
                str(row["error_type"]), str(row["error_type"])
            )
            for row in rows
            if row["error_type"]
        )
        success_rate = successful * 100 / total if total else 0.0
        timeline = "".join(
            "<tr>"
            f"<td>#{int(row['id'])}</td>"
            f"<td>{html.escape(str(row['connection_name']))}</td>"
            f"<td>{html.escape(str(row['started_at']))}</td>"
            f"<td><span class='state "
            f"{html.escape(str(row['result_status']))}'>"
            f"{html.escape(str(row['status_label']))}</span></td>"
            f"<td>{int(row['files_downloaded'] or 0)}</td>"
            f"<td>{_format_bytes(int(row['bytes_downloaded'] or 0))}</td>"
            "</tr>"
            for row in rows[:250]
        )
        error_items = "".join(
            f"<li><strong>{html.escape(cause)}</strong><span>{count}</span></li>"
            for cause, count in errors.most_common(8)
        ) or "<li><span>Sin errores registrados</span><span>0</span></li>"
        generated = self.now().astimezone(timezone.utc).isoformat()
        title_suffix = f" · {html.escape(client)}" if client else ""
        return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reporte Recolecta{title_suffix}</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f3f6fa;color:#152033}}
main{{max-width:1100px;margin:auto;padding:32px}} header{{background:#10243a;color:white;padding:28px;border-radius:12px}}
h1{{margin:4px 0}} .muted{{color:#64748b}} .cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}}
.card,section{{background:white;border:1px solid #d9e1ea;border-radius:10px;padding:18px}} .card strong{{display:block;font-size:26px;margin-top:8px}}
.grid{{display:grid;grid-template-columns:1fr 2fr;gap:14px}} ul{{list-style:none;padding:0}} li{{display:flex;justify-content:space-between;padding:9px;border-bottom:1px solid #e7edf4}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{padding:10px;text-align:left;border-bottom:1px solid #e7edf4}}
.state{{font-weight:700}} .ok,.completed{{color:#087c55}} .failed,.cancelled{{color:#b42336}} .partial,.running{{color:#9a6500}} .no_files,.no_changes{{color:#175cd3}}
@media(max-width:760px){{.cards,.grid{{grid-template-columns:1fr}}}} @media print{{body{{background:white}}main{{padding:0}}}}
</style></head><body><main>
<header><small>REPORTE OPERATIVO</small><h1>Recolecta{title_suffix}</h1>
<p>Últimos {days} días · generado {html.escape(generated)}</p></header>
<div class="cards">
<div class="card"><span>Corridas</span><strong>{total}</strong></div>
<div class="card"><span>Ejecuciones sin error</span><strong>{success_rate:.1f}%</strong></div>
<div class="card"><span>Archivos</span><strong>{files}</strong></div>
<div class="card"><span>Volumen</span><strong>{_format_bytes(volume)}</strong></div>
</div><div class="grid"><section><h2>Errores principales</h2><ul>{error_items}</ul></section>
<section><h2>Timeline</h2><div style="overflow:auto"><table><thead><tr>
<th>Corrida</th><th>Conexión</th><th>Inicio UTC</th><th>Estado</th><th>Archivos</th><th>Volumen</th>
</tr></thead><tbody>{timeline}</tbody></table></div></section></div>
</main></body></html>"""

    def support_bundle(self, *, days: int = 7) -> Path:
        self.paths.exports.mkdir(parents=True, exist_ok=True)
        stamp = self.now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.paths.exports / (
            f"recolecta-support-{stamp}-{secrets.token_hex(3)}.zip"
        )
        cutoff = self.now().astimezone(timezone.utc) - timedelta(days=days)
        with zipfile.ZipFile(
            destination, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("exports/runs.csv", self.runs_csv(days=days))
            archive.writestr("exports/files.csv", self.files_csv(
                date_from=cutoff.date().isoformat()
            ))
            archive.writestr(
                "exports/configuration.json",
                json.dumps(
                    self.safe_configuration(),
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            archive.writestr(
                "exports/report.html",
                self.html_report(days=days),
            )
            for path in sorted(self.paths.logs.glob("app.log*")):
                if path.is_file():
                    archive.write(path, f"logs/{path.name}")
            for path in self.run_logs.list_since(cutoff):
                archive.write(path, f"logs/runs/{path.name}")
        return destination

    def _runs(self, *, days: int) -> list[dict[str, Any]]:
        if days < 1 or days > 3650:
            raise ValueError("El rango debe estar entre 1 y 3650 días.")
        cutoff = self.now().astimezone(timezone.utc) - timedelta(days=days)
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.runs.list_runs(
                date_from=cutoff.date().isoformat(),
                limit=500,
                offset=offset,
            )
            rows.extend(enrich_run(row) for row in page)
            if len(page) < 500:
                return rows
            offset += len(page)


def _csv_text(
    rows: list[dict[str, Any]], fields: tuple[str, ...]
) -> str:
    stream = io.StringIO()
    stream.write("\ufeff")
    writer = csv.DictWriter(
        stream,
        fieldnames=fields,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {key: _csv_safe(row.get(key)) for key in fields}
        )
    return stream.getvalue()


def _csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _sensitive_key(key: str) -> bool:
    normalized = key.casefold()
    return any(
        token in normalized
        for token in ("password", "secret", "passphrase", "token", "credential")
    )


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    units = ("B", "KB", "MB", "GB", "TB")
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    return f"{size:.1f} {unit}"
