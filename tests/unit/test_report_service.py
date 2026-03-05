from flowtrack.services.report_service import ReportService


class TestReportService:
    def test_generate_empty_report(self, db):
        svc = ReportService(db)
        report = svc.generate(period="week")

        assert report.space.total_sessions == 0
        assert report.space.flow_time_ratio == 0.0
        assert report.dora.deployment_frequency == 0.0
        assert report.dora.mttr_hours == 0.0
