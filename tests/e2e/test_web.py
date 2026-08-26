"""الواجهة من طرف إلى طرف: الصلاحيات، الحماية، والتصدير (SRS §12، §13، AC-011)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.security import hash_public_token, new_public_token
from app.models import Confidence, MonthlyReport, ZoneRuntimeEvent
from app.services.report_generator import generate_monthly_report
from tests.conftest import ADMIN_PASSWORD

MUSCAT_OFFSET = timedelta(hours=4)


def muscat(year, month, day, hour=0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC) - MUSCAT_OFFSET


@pytest.fixture()
def report(db, controller, zones) -> MonthlyReport:
    start = muscat(2026, 7, 5, 6)
    db.add(
        ZoneRuntimeEvent(
            zone_id=zones[0].id,
            started_at=start,
            ended_at=start + timedelta(hours=1),
            last_running_at=start + timedelta(hours=1),
            runtime_seconds=3600,
            confidence=Confidence.HIGH,
            flow_rate_lpm_snapshot=Decimal("140.00"),
            flow_min_lpm_snapshot=Decimal("80.00"),
            flow_max_lpm_snapshot=Decimal("200.00"),
            water_liters_estimate=Decimal("8400.00"),
        )
    )
    db.commit()
    entry = generate_monthly_report(db, controller, year=2026, month=7)
    db.commit()
    return entry


class TestAuthentication:
    def test_pages_redirect_anonymous_visitors_to_login(self, client):
        for path in ("/dashboard", "/zones", "/events", "/reports", "/system/health"):
            response = client.get(path)
            assert response.status_code == 303
            assert response.headers["location"] == "/login"

    def test_the_api_answers_401_json_not_a_redirect(self, client):
        response = client.get("/api/v1/zones")
        assert response.status_code == 401
        assert response.json()["detail"]

    def test_health_is_public_for_monitoring(self, client):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] in {"ok", "degraded"}

    def test_login_and_logout(self, client, admin_user):
        response = client.post(
            "/login", data={"username": "admin", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 303
        assert client.get("/dashboard").status_code == 200

        page = client.get("/zones").text
        token = page.split('name="csrf_token" value="')[1].split('"')[0]
        assert client.post("/logout", data={"csrf_token": token}).status_code == 303
        assert client.get("/dashboard").status_code == 303

    def test_a_wrong_password_gives_one_generic_message(self, client, admin_user):
        response = client.post("/login", data={"username": "admin", "password": "nope"})
        assert response.status_code == 401
        assert "غير صحيحة" in response.text
        assert "admin" not in response.text.split("<title>")[1]

    def test_an_unknown_user_looks_the_same_as_a_wrong_password(self, client, admin_user):
        unknown = client.post("/login", data={"username": "ghost", "password": "nope"})
        wrong = client.post("/login", data={"username": "admin", "password": "nope"})
        assert unknown.status_code == wrong.status_code == 401

    def test_repeated_failures_are_rate_limited(self, client, admin_user):
        codes = [
            client.post("/login", data={"username": "admin", "password": "x"}).status_code
            for _ in range(7)
        ]
        assert 429 in codes


class TestAuthorisation:
    def test_a_viewer_cannot_change_a_zone(self, client, viewer_user, zones):
        client.post("/login", data={"username": "viewer", "password": ADMIN_PASSWORD})
        page = client.get("/zones")
        assert page.status_code == 200
        # لا يظهر نموذج التعديل أصلًا لغير الإدارة.
        assert "التعديل متاح للإدارة فقط" in page.text

        response = client.patch(
            f"/api/v1/zones/{zones[0].id}",
            json={"flow_rate_lpm": 200, "reason": "محاولة"},
            headers={"X-CSRF-Token": "whatever"},
        )
        assert response.status_code == 403

    def test_an_admin_can_change_a_zone_and_it_is_audited(self, admin_client, db, zones):
        page = admin_client.get("/zones").text
        token = page.split('name="csrf_token" value="')[1].split('"')[0]
        response = admin_client.post(
            f"/zones/{zones[0].id}",
            data={
                "csrf_token": token,
                "display_name_ar": "المسطح الأمامي",
                "flow_rate_lpm": "155.5",
                "flow_min_lpm": "80",
                "flow_max_lpm": "200",
                "calibration_method": "manual",
                "reason": "قياس بدلو مدرّج ثلاث مرات",
            },
        )
        assert response.status_code == 200
        db.expire_all()
        zone = db.get(type(zones[0]), zones[0].id)
        assert float(zone.flow_rate_lpm) == 155.5
        assert zone.is_calibrated

        from app.models import AuditLog

        entry = db.query(AuditLog).filter(AuditLog.action == "zone.updated").one()
        assert entry.reason == "قياس بدلو مدرّج ثلاث مرات"
        assert entry.before_json["flow_rate_lpm"] == 140.0
        assert entry.after_json["flow_rate_lpm"] == 155.5

    def test_an_out_of_order_flow_range_is_rejected(self, admin_client, zones):
        page = admin_client.get("/zones").text
        token = page.split('name="csrf_token" value="')[1].split('"')[0]
        response = admin_client.post(
            f"/zones/{zones[0].id}",
            data={
                "csrf_token": token,
                "flow_rate_lpm": "50",
                "flow_min_lpm": "80",
                "flow_max_lpm": "200",
                "calibration_method": "manual",
            },
        )
        assert response.status_code == 400


class TestCsrf:
    def test_a_post_without_a_token_is_refused(self, admin_client, zones):
        response = admin_client.post(
            f"/zones/{zones[0].id}",
            data={"flow_rate_lpm": "150", "flow_min_lpm": "80", "flow_max_lpm": "200"},
        )
        assert response.status_code == 403

    def test_a_token_from_another_session_is_refused(self, admin_client, zones):
        from app.core.security import csrf_token_for

        response = admin_client.post(
            f"/zones/{zones[0].id}",
            data={
                "csrf_token": csrf_token_for("some-other-session"),
                "flow_rate_lpm": "150",
                "flow_min_lpm": "80",
                "flow_max_lpm": "200",
            },
        )
        assert response.status_code == 403


class TestReports:
    def test_the_report_page_renders_in_arabic(self, admin_client, report):
        response = admin_client.get("/reports/2026-07")
        assert response.status_code == 200
        assert 'dir="rtl"' in response.text
        assert "تقرير الري الشهري" in response.text
        assert "المنهجية" in response.text

    def test_json_pdf_and_csv_agree_on_the_totals(self, admin_client, report):
        payload = admin_client.get("/api/v1/reports/monthly/2026-07").json()
        assert payload["metrics"]["water_estimate_liters"] == pytest.approx(8400.0)

        csv_response = admin_client.get("/api/v1/reports/monthly/2026-07/csv")
        assert csv_response.status_code == 200
        assert csv_response.content.startswith(b"\xef\xbb\xbf")
        assert "8400.00" in csv_response.content.decode("utf-8-sig")

        pdf_response = admin_client.get("/api/v1/reports/monthly/2026-07/pdf")
        assert pdf_response.status_code == 200
        assert pdf_response.content.startswith(b"%PDF")
        assert "ALMOHANA-irrigation-report-2026-07.pdf" in pdf_response.headers[
            "content-disposition"
        ]

    def test_a_missing_month_is_a_clear_404(self, admin_client, controller):
        response = admin_client.get("/api/v1/reports/monthly/2020-01")
        assert response.status_code == 404
        assert "ولّده" in response.json()["detail"]

    def test_a_malformed_month_is_rejected(self, admin_client, controller):
        assert admin_client.get("/api/v1/reports/monthly/july").status_code == 400

    def test_an_admin_can_regenerate_from_the_page(self, admin_client, report, db):
        page = admin_client.get("/reports/2026-07").text
        token = page.split('name="csrf_token" value="')[1].split('"')[0]
        response = admin_client.post(
            "/reports/2026-07/regenerate", data={"csrf_token": token}
        )
        assert response.status_code == 200
        assert db.query(MonthlyReport).count() == 1


class TestPublicLink:
    def test_a_valid_token_shows_the_report_without_login(self, client, db, report):
        token = new_public_token()
        report.public_token_hash = hash_public_token(token)
        report.public_token_expires_at = datetime.now(tz=UTC) + timedelta(days=10)
        db.commit()

        response = client.get(f"/r/{token}")
        assert response.status_code == 200
        assert "تقرير الري الشهري" in response.text
        assert response.headers["x-robots-tag"].startswith("noindex")
        # الصفحة العامة لا تحمل أزرار الإدارة ولا شريط التنقل.
        assert "إعادة التوليد" not in response.text
        assert "/logout" not in response.text

    def test_an_expired_token_is_refused(self, client, db, report):
        token = new_public_token()
        report.public_token_hash = hash_public_token(token)
        report.public_token_expires_at = datetime.now(tz=UTC) - timedelta(minutes=1)
        db.commit()
        assert client.get(f"/r/{token}").status_code == 404

    def test_an_unknown_token_is_refused(self, client, report):
        assert client.get(f"/r/{new_public_token()}").status_code == 404

    def test_issuing_a_new_link_invalidates_the_previous_one(self, admin_client, db, report):
        page = admin_client.get("/reports/2026-07").text
        token = page.split('name="csrf_token" value="')[1].split('"')[0]
        first = admin_client.post("/reports/2026-07/share", data={"csrf_token": token})
        assert first.status_code == 200
        old_hash = db.get(MonthlyReport, report.id).public_token_hash

        admin_client.post("/reports/2026-07/share", data={"csrf_token": token})
        db.expire_all()
        assert db.get(MonthlyReport, report.id).public_token_hash != old_hash


class TestSecretsAndHeaders:
    def test_ac_011_no_secret_leaks_into_any_page(self, admin_client, report):
        secrets = ("test-api-key-abcdef", "test-secret-key-not-used-in-production-000000")
        paths = (
            "/dashboard", "/zones", "/events", "/reports", "/reports/2026-07",
            "/system/health", "/settings/integrations", "/settings/pump",
            "/api/v1/reports/monthly/2026-07", "/api/v1/zones", "/api/v1/health",
        )
        for path in paths:
            body = admin_client.get(path).text
            for secret in secrets:
                assert secret not in body, f"{secret} تسرّب في {path}"

    def test_the_integrations_page_names_the_variable_not_the_value(self, admin_client):
        body = admin_client.get("/settings/integrations").text
        assert "HYDRAWISE_API_KEY" in body
        assert "لا يُعرض" in body

    def test_security_headers_are_present(self, client):
        headers = client.get("/login").headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in headers["content-security-policy"]

    def test_no_external_asset_is_referenced(self, admin_client, report):
        # CSP يمنع الطلبات الخارجية؛ الصفحة يجب أن تكون مكتفية ذاتيًا أصلًا.
        for path in ("/dashboard", "/reports/2026-07"):
            body = admin_client.get(path).text
            assert "https://cdn" not in body
            assert "<script src=" not in body


class TestNoControlSurface:
    """SRS §19: لا يوجد في التطبيق سطح تحكّم بالمضخة أصلًا."""

    def test_no_route_in_the_application_commands_a_zone(self):
        from app.main import app

        def paths(router) -> list[str]:
            found: list[str] = []
            for route in getattr(router, "routes", []):
                path = getattr(route, "path", None)
                if isinstance(path, str):
                    found.append(path)
                found.extend(paths(route))
                inner = getattr(route, "original_router", None)
                if inner is not None:
                    found.extend(paths(inner))
            return found

        control_words = ("setzone", "/run", "/stop", "/suspend", "/start")
        offenders = [
            path
            for path in paths(app)
            if any(word in path.lower() for word in control_words)
        ]
        assert offenders == [], offenders

    def test_a_guessed_control_path_never_succeeds(self, admin_client, zones):
        for path in (
            f"/api/v1/zones/{zones[0].id}/run",
            "/api/v1/zones/run",
            "/api/v1/setzone",
        ):
            assert admin_client.post(path, json={}).status_code >= 400

    def test_the_zones_page_offers_calibration_not_control(self, admin_client, zones):
        body = admin_client.get("/zones").text
        assert "معايرة" in body
        # لا نموذج يرسل أمرًا: كل نماذج الصفحة تحفظ إعدادات المحبس فقط.
        actions = [
            fragment.split('"')[0]
            for fragment in body.split('action="')[1:]
        ]
        assert all("/zones/" in action or action in ("/logout",) for action in actions)
