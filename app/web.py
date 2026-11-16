"""Flask web interface: upload batches, inspect them, download GeoJSON."""
from __future__ import annotations

import io
import os

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template,
    send_file, url_for, request,
)
from werkzeug.utils import secure_filename

from . import config
from .db import connect, init_db
from .ingestion import BatchRejected, ingest_stream
from .osm_parser import OSMParseError
from .roads import batch_summary, build_road_layer, road_geojson
from . import connectivity
from .connectivity import ConnectivityError


def create_app(db_path=None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    # Explicit argument wins (used by tests); otherwise fall back to config.
    resolved_db = str(db_path) if db_path is not None else str(config.DB_PATH)
    app.config["DB_PATH"] = resolved_db
    init_db(resolved_db)

    def get_conn():
        if "db" not in g:
            g.db = connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_conn(exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.route("/health")
    def health():
        return jsonify(status="ok")

    @app.route("/")
    def index():
        conn = get_conn()
        batches = conn.execute(
            "SELECT * FROM batches ORDER BY id DESC"
        ).fetchall()
        stats = []
        for b in batches:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM object_versions WHERE batch_id = ?",
                (b["id"],),
            ).fetchone()
            stats.append(row["n"])
        return render_template("index.html", batches=batches, row_counts=stats)

    @app.route("/upload", methods=["POST"])
    def upload():
        upload = request.files.get("osm_file")
        if upload is None or upload.filename == "":
            flash("未选择文件，请选择 .osm 或 .osc 文件后再上传。", "error")
            return redirect(url_for("index"))

        filename = secure_filename(upload.filename)
        ext = os.path.splitext(filename)[1].lower()
        if ext not in config.ALLOWED_EXTENSIONS:
            flash(f"不支持的文件类型 {ext or '(无扩展名)'}，仅接受 .osm / .osc。", "error")
            return redirect(url_for("index"))

        # Read into memory so the parser always sees a seekable stream and so
        # a failed upload never leaves temporary files behind.
        data = upload.read()
        if not data:
            flash("上传的文件为空。", "error")
            return redirect(url_for("index"))

        conn = get_conn()
        try:
            result = ingest_stream(conn, filename, io.BytesIO(data))
        except (BatchRejected, OSMParseError) as exc:
            reason = str(exc)
            flash(f"批次被拒绝：{reason}", "error")
            return redirect(url_for("index"))
        except Exception as exc:  # pragma: no cover - defensive
            app.logger.exception("ingestion failed")
            flash(f"服务端异常，批次已回滚：{exc!r}", "error")
            return redirect(url_for("index"))

        flash(
            f"批次 #{result.batch_id}（{filename}）已整体生效：新增 node "
            f"{result.inserted_nodes} 个、way {result.inserted_ways} 个；"
            f"忽略旧版 {result.ignored_old} 个，跳过重复同版 {result.duplicates} 个。",
            "success",
        )
        return redirect(url_for("batch_detail", batch_id=result.batch_id))

    @app.route("/batches/<int:batch_id>")
    def batch_detail(batch_id: int):
        conn = get_conn()
        batch = conn.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            abort(404)

        roads = pending = summary = None
        if batch["status"] == "committed":
            roads, pending = build_road_layer(conn, batch_id)
            summary = batch_summary(conn, batch_id)
        return render_template(
            "batch.html", batch=batch, roads=roads, pending=pending, summary=summary
        )

    @app.route("/batches/<int:batch_id>/geojson")
    def geojson(batch_id: int):
        conn = get_conn()
        batch = conn.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            abort(404)
        if batch["status"] != "committed":
            abort(409, description="该批次未成功提交，无法导出 GeoJSON")

        # Snapshot read only: never opens a write transaction, so concurrent
        # uploads can keep committing while the export is produced.
        roads, _pending = build_road_layer(conn, batch_id)
        payload = road_geojson(roads)
        raw = jsonify(payload).get_data(as_text=False)
        return send_file(
            io.BytesIO(raw),
            mimetype="application/geo+json",
            as_attachment=True,
            download_name=f"batch_{batch_id}_highways.geojson",
        )

    # ------------------------------------------------------------------
    # Connectivity quality check
    # ------------------------------------------------------------------

    def _committed_batches(conn):
        return conn.execute(
            "SELECT id, filename, created_at FROM batches "
            "WHERE status = 'committed' ORDER BY id"
        ).fetchall()

    def _connectivity_context(conn):
        return {
            "pairs": connectivity.list_pairs(conn),
            "batches": _committed_batches(conn),
            "max_pairs": config.MAX_WATCH_PAIRS,
            "running_rule_version": config.CONNECTIVITY_RULE_VERSION,
            "rule_version": connectivity.current_rule_version(conn)["version"],
            "reports": connectivity.list_reports(conn),
        }

    @app.route("/connectivity")
    def connectivity_index():
        conn = get_conn()
        return render_template("connectivity.html", **_connectivity_context(conn))

    @app.route("/connectivity/pairs", methods=["POST"])
    def connectivity_add_pair():
        conn = get_conn()
        try:
            connectivity.add_pair(
                conn,
                request.form.get("start_node_id", ""),
                request.form.get("end_node_id", ""),
                request.form.get("label", ""),
            )
            flash("起终点 node 组已保存。", "success")
        except ConnectivityError as exc:
            flash(f"未保存：{exc}", "error")
        return redirect(url_for("connectivity_index"))

    @app.route("/connectivity/pairs/<int:pair_id>/delete", methods=["POST"])
    def connectivity_delete_pair(pair_id: int):
        conn = get_conn()
        connectivity.delete_pair(conn, pair_id)
        flash(f"起终点组 #{pair_id} 已删除（历史报告保留快照）。", "success")
        return redirect(url_for("connectivity_index"))

    @app.route("/connectivity/rule-version", methods=["POST"])
    def connectivity_rule_version():
        conn = get_conn()
        version = (request.form.get("rule_version") or "").strip()
        try:
            connectivity.register_rule_version(conn, version)
        except ConnectivityError as exc:
            flash(f"未登记规则版本：{exc}", "error")
            return redirect(url_for("connectivity_index"))

        newest = connectivity.current_rule_version(conn)["version"]
        if version != newest:
            flash(
                f"规则版本 {version} 此前已登记，且不是当前最新版本；"
                f"最新规则版本仍为 {newest}，已追加登记的版本不会回退规则历史。",
                "error",
            )
        elif version != config.CONNECTIVITY_RULE_VERSION:
            flash(
                f"规则版本 {version} 已登记为最新；当前运行的代码实现的是 "
                f"{config.CONNECTIVITY_RULE_VERSION}，因此此后由本代码生成的报告"
                "都会标为过期，直到部署实现该规则的版本。",
                "error",
            )
        else:
            flash(
                f"规则版本 {version} 已登记为最新；此前生成的报告已标记为过期，"
                "仅供留档，不再作为当前结论。",
                "success",
            )
        return redirect(url_for("connectivity_index"))

    @app.route("/connectivity/reports", methods=["POST"])
    def connectivity_create_report():
        conn = get_conn()

        def form_int(field: str, label: str) -> int:
            raw = (request.form.get(field) or "").strip()
            try:
                return int(raw)
            except ValueError as exc:
                raise ConnectivityError(f"{label}必须是整数批次号") from exc

        try:
            from_batch = form_int("from_batch_id", "起始批次")
            to_batch = form_int("to_batch_id", "结束批次")
            report_id = connectivity.create_report(conn, from_batch, to_batch)
        except ConnectivityError as exc:
            flash(f"未生成报告：{exc}", "error")
            return redirect(url_for("connectivity_index"))
        return redirect(url_for("connectivity_report", report_id=report_id))

    @app.route("/connectivity/reports/<int:report_id>")
    def connectivity_report(report_id: int):
        conn = get_conn()
        report, items, summary = connectivity.load_report(conn, report_id)
        if report is None:
            abort(404)
        return render_template(
            "connectivity_report.html",
            report=report,
            items=items,
            summary=summary,
            category_labels=connectivity.CATEGORY_LABELS,
            state_labels=connectivity.STATE_LABELS,
            endpoint_labels=connectivity.ENDPOINT_LABELS,
            newest_rule=connectivity.current_rule_version(conn)["version"],
            running_rule_version=config.CONNECTIVITY_RULE_VERSION,
        )

    @app.errorhandler(413)
    def too_large(_e):
        flash(f"文件超过大小上限 {config.MAX_CONTENT_LENGTH} 字节，已拒绝。", "error")
        return redirect(url_for("index"))

    return app
