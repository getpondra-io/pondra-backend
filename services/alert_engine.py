"""
services/alert_engine.py
─────────────────────────
Daily email summary service using Gmail SMTP.
Runs a background task that fires every day at 07:00.
"""

import asyncio
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from typing import Optional
import structlog

from config.settings import get_settings
from db.database import AsyncSessionLocal, User, Farm, AIDecisionDB, Alert
from sqlalchemy import select, func, text

settings = get_settings()
log = structlog.get_logger(__name__)


def _severity_emoji(severity: str) -> str:
    return {"ok": "✅", "warning": "⚠️", "critical": "🚨"}.get(severity, "✅")


def _build_email_html(user_name: str, summaries: list[dict]) -> str:
    date_str = datetime.now().strftime("%B %d, %Y")
    farm_sections = ""
    for farm in summaries:
        status_color = {"ok": "#0d9488", "warning": "#d97706", "critical": "#dc2626"}.get(farm["worst_severity"], "#0d9488")
        sensor_rows = "".join([
            f'<tr><td style="padding:6px 0;color:#94a3b8;font-size:13px;">{p}</td>'
            f'<td style="padding:6px 0;color:#e2e8f0;font-size:13px;text-align:right;">{v} {u}</td></tr>'
            for p, v, u in farm["sensors"]
        ])
        alert_section = ""
        if farm["alerts"]:
            items = "".join([f'<li style="color:#fbbf24;font-size:13px;margin:4px 0;">⚠️ {a}</li>' for a in farm["alerts"]])
            alert_section = f'<div style="background:#1c1400;border:1px solid #92400e;border-radius:8px;padding:12px;margin-top:12px;"><p style="color:#fbbf24;font-size:12px;font-weight:600;margin:0 0 6px;">ALERTS</p><ul style="margin:0;padding-left:16px;">{items}</ul></div>'

        farm_sections += f'''
        <div style="background:#1e293b;border:1px solid #334155;border-radius:12px;padding:20px;margin-bottom:16px;">
          <h3 style="color:#f1f5f9;font-size:15px;font-weight:600;margin:0 0 4px;">{farm["name"]}</h3>
          <p style="color:#64748b;font-size:12px;margin:0 0 12px;">{farm["farm_id"]} · {farm["species"]} · <span style="color:{status_color};font-weight:600;">{_severity_emoji(farm["worst_severity"])} {farm["worst_severity"].upper()}</span></p>
          <table style="width:100%;border-collapse:collapse;">{sensor_rows}</table>
          <p style="color:#64748b;font-size:12px;margin:10px 0 0;">{farm["decision_count"]} AI decisions · {farm["alert_count"]} alerts in last 24h</p>
          {alert_section}
        </div>'''

    return f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:560px;margin:0 auto;padding:32px 16px;">
  <div style="text-align:center;margin-bottom:28px;">
    <h1 style="color:#5eead4;font-size:22px;font-weight:700;margin:0;">🐟 Pondra</h1>
    <p style="color:#64748b;font-size:13px;margin:4px 0 0;">Daily Farm Summary · {date_str}</p>
  </div>
  <p style="color:#94a3b8;font-size:14px;margin:0 0 20px;">Good morning{", " + user_name if user_name else ""}! Here's your farm status for the last 24 hours.</p>
  {farm_sections}
  <div style="text-align:center;margin-top:28px;padding-top:20px;border-top:1px solid #1e293b;">
    <p style="color:#475569;font-size:12px;margin:0;">Pondra · AI for fish &amp; shrimp farms<br>
    <a href="http://localhost:3000/dashboard" style="color:#0d9488;">Open Dashboard →</a></p>
  </div>
</div>
</body></html>'''


def _build_email_text(user_name: str, summaries: list[dict]) -> str:
    lines = [f"Pondra — Daily Farm Summary — {datetime.now().strftime('%B %d, %Y')}", ""]
    if user_name:
        lines.append(f"Good morning, {user_name}!")
    lines.append("")
    for farm in summaries:
        lines += [f"{'='*40}", f"{farm['name']} ({farm['farm_id']}) — {farm['worst_severity'].upper()}"]
        for p, v, u in farm["sensors"]:
            lines.append(f"  {p}: {v} {u}")
        lines.append(f"  AI decisions: {farm['decision_count']}, Alerts: {farm['alert_count']}")
        lines += [f"  ⚠ {a}" for a in farm["alerts"]]
        lines.append("")
    lines.append("Open dashboard: http://localhost:3000/dashboard")
    return "\n".join(lines)


def _send_gmail(to_email: str, subject: str, html_body: str, text_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.gmail_user
    msg["To"] = to_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(settings.gmail_user, settings.gmail_app_password)
        server.sendmail(settings.gmail_user, to_email, msg.as_string())


async def _collect_farm_summary(db, farm: Farm) -> dict:
    since = datetime.utcnow() - timedelta(hours=24)
    result = await db.execute(text(
        'SELECT "do", ph, nh3, temp FROM sensor_readings WHERE farm_id = :fid ORDER BY time DESC LIMIT 1'
    ), {"fid": farm.farm_id})
    latest = result.fetchone()
    sensors = []
    if latest:
        if latest.do   is not None: sensors.append(("Dissolved O₂", f"{latest.do:.1f}", "mg/L"))
        if latest.ph   is not None: sensors.append(("pH", f"{latest.ph:.1f}", ""))
        if latest.nh3  is not None: sensors.append(("Ammonia NH₃", f"{latest.nh3:.3f}", "ppm"))
        if latest.temp is not None: sensors.append(("Temperature", f"{latest.temp:.1f}", "°C"))

    dec_result = await db.execute(
        select(AIDecisionDB.severity, func.count(AIDecisionDB.id).label("cnt"))
        .where(AIDecisionDB.farm_id == farm.farm_id, AIDecisionDB.decided_at > since)
        .group_by(AIDecisionDB.severity)
    )
    dec_rows = dec_result.fetchall()
    decision_count = sum(r.cnt for r in dec_rows)
    rank = {"critical": 3, "warning": 2, "ok": 1}
    worst_severity = max((r.severity for r in dec_rows), key=lambda s: rank.get(s, 0), default="ok")

    alert_result = await db.execute(
        select(Alert).where(Alert.farm_id == farm.farm_id, Alert.is_resolved == False)
        .order_by(Alert.created_at.desc()).limit(5)
    )
    alerts = alert_result.scalars().all()
    return {
        "farm_id": farm.farm_id, "name": farm.name, "species": farm.species_id or "unknown",
        "sensors": sensors, "decision_count": decision_count, "alert_count": len(alerts),
        "alerts": [a.message for a in alerts], "worst_severity": worst_severity,
    }


async def _send_daily_summaries():
    log.info("alert_engine.daily_summary.starting")
    if not settings.gmail_user or not settings.gmail_app_password:
        log.warning("alert_engine.no_gmail_config", msg="Set GMAIL_USER and GMAIL_APP_PASSWORD in .env")
        return

    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User).where(User.is_active == True))).scalars().all()
        for user in users:
            try:
                farms = (await db.execute(
                    select(Farm).where(Farm.owner_id == user.id, Farm.is_active == True)
                )).scalars().all()
                if not farms:
                    continue

                summaries = [await _collect_farm_summary(db, f) for f in farms]
                user_name = user.full_name or user.email.split("@")[0]
                subject = f"🐟 Pondra Daily Summary — {datetime.now().strftime('%b %d')}"
                html_body = _build_email_html(user_name, summaries)
                text_body = _build_email_text(user_name, summaries)

                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: _send_gmail(user.email, subject, html_body, text_body))
                log.info("alert_engine.email_sent", user=user.email, farms=len(farms))
            except Exception as e:
                log.error("alert_engine.email_failed", user=user.email, error=str(e))

    log.info("alert_engine.daily_summary.done")


class AlertEngine:
    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop())
        log.info("alert_engine.started", schedule="daily at 07:00")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("alert_engine.stopped")

    async def _scheduler_loop(self):
        while self._running:
            now = datetime.now()
            next_run = now.replace(hour=7, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            wait_seconds = (next_run - now).total_seconds()
            log.info("alert_engine.next_run", at=next_run.strftime("%Y-%m-%d %H:%M"), in_hours=f"{wait_seconds/3600:.1f}h")
            try:
                await asyncio.sleep(wait_seconds)
                if self._running:
                    await _send_daily_summaries()
            except asyncio.CancelledError:
                break

    async def send_now(self):
        await _send_daily_summaries()
