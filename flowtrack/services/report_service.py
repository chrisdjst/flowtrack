from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session as DbSession

from flowtrack.models.event import EventType
from flowtrack.models.session import SessionType
from flowtrack.repositories.deployment_repo import DeploymentRepository
from flowtrack.repositories.event_repo import EventRepository
from flowtrack.repositories.incident_repo import IncidentRepository
from flowtrack.repositories.session_repo import SessionRepository


@dataclass
class SpaceMetrics:
    total_sessions: int = 0
    sessions_per_day: float = 0.0
    flow_time_ratio: float = 0.0
    blocking_ratio: float = 0.0
    interrupts_per_session: float = 0.0
    review_sessions: int = 0
    avg_review_duration_min: float = 0.0
    test_sessions: int = 0
    change_failure_rate: float = 0.0


@dataclass
class DoraMetrics:
    deployment_frequency: float = 0.0
    lead_time_hours: float = 0.0
    change_failure_rate: float = 0.0
    mttr_hours: float = 0.0


@dataclass
class Report:
    period_start: datetime = field(default_factory=datetime.now)
    period_end: datetime = field(default_factory=datetime.now)
    space: SpaceMetrics = field(default_factory=SpaceMetrics)
    dora: DoraMetrics = field(default_factory=DoraMetrics)


class ReportService:
    def __init__(self, db: DbSession) -> None:
        self.session_repo = SessionRepository(db)
        self.event_repo = EventRepository(db)
        self.deploy_repo = DeploymentRepository(db)
        self.incident_repo = IncidentRepository(db)

    def generate(self, period: str = "week") -> Report:
        now = datetime.now()
        if period == "month":
            start = now - timedelta(days=30)
        elif period == "sprint":
            start = now - timedelta(days=14)
        else:
            start = now - timedelta(days=7)

        report = Report(period_start=start, period_end=now)
        report.space = self._calc_space(start, now)
        report.dora = self._calc_dora(start, now)
        return report

    def _calc_space(self, start: datetime, end: datetime) -> SpaceMetrics:
        sessions = self.session_repo.list_by_period(start, end)
        events = self.event_repo.list_by_period(start, end)
        deployments = self.deploy_repo.list_by_period(start, end)
        incidents = self.incident_repo.list_by_period(start, end)

        days = max((end - start).days, 1)
        total = len(sessions)

        # Flow time ratio: active time / total session time
        total_session_secs = 0.0
        total_block_secs = 0.0
        total_interrupt_secs = 0.0
        for s in sessions:
            session_end = s.ended_at or end
            total_session_secs += (session_end - s.started_at).total_seconds()

        for e in events:
            duration = ((e.ended_at or end) - e.started_at).total_seconds()
            if e.event_type == EventType.BLOCK_START:
                total_block_secs += duration
            elif e.event_type == EventType.INTERRUPT_START:
                total_interrupt_secs += duration

        active_secs = total_session_secs - total_block_secs - total_interrupt_secs
        flow_ratio = active_secs / total_session_secs if total_session_secs > 0 else 0.0
        block_ratio = total_block_secs / total_session_secs if total_session_secs > 0 else 0.0

        block_count = sum(1 for e in events if e.event_type == EventType.BLOCK_START)
        interrupt_count = sum(1 for e in events if e.event_type == EventType.INTERRUPT_START)

        review_sessions = [s for s in sessions if s.type == SessionType.REVIEW]
        review_durations = []
        for s in review_sessions:
            if s.ended_at:
                review_durations.append((s.ended_at - s.started_at).total_seconds() / 60)

        test_sessions = [s for s in sessions if s.type == SessionType.TESTING]

        total_deploys = len(deployments)
        deploy_ids_with_incident = {i.deployment_id for i in incidents if i.deployment_id}
        failed_deploys = sum(1 for d in deployments if d.id in deploy_ids_with_incident)
        cfr = failed_deploys / total_deploys if total_deploys > 0 else 0.0

        return SpaceMetrics(
            total_sessions=total,
            sessions_per_day=total / days,
            flow_time_ratio=flow_ratio,
            blocking_ratio=block_ratio,
            interrupts_per_session=interrupt_count / total if total > 0 else 0.0,
            review_sessions=len(review_sessions),
            avg_review_duration_min=(
                sum(review_durations) / len(review_durations) if review_durations else 0.0
            ),
            test_sessions=len(test_sessions),
            change_failure_rate=cfr,
        )

    def _calc_dora(self, start: datetime, end: datetime) -> DoraMetrics:
        deployments = self.deploy_repo.list_by_period(start, end)
        incidents = self.incident_repo.list_resolved_by_period(start, end)

        days = max((end - start).days, 1)
        total_deploys = len(deployments)

        # Lead time: for each deploy with a ticket, find earliest dev session for that ticket
        lead_times = []
        for deploy in deployments:
            if deploy.ticket_id:
                dev_sessions = self.session_repo.list_by_ticket(deploy.ticket_id)
                if dev_sessions:
                    earliest = dev_sessions[0].started_at
                    lt = (deploy.deployed_at - earliest).total_seconds() / 3600
                    lead_times.append(lt)

        # Change failure rate
        deploy_ids_with_incident = set()
        all_incidents = self.incident_repo.list_by_period(start, end)
        for i in all_incidents:
            if i.deployment_id:
                deploy_ids_with_incident.add(i.deployment_id)
        failed = sum(1 for d in deployments if d.id in deploy_ids_with_incident)

        # MTTR
        mttr_values = []
        for i in incidents:
            if i.resolved_at:
                mttr_values.append((i.resolved_at - i.started_at).total_seconds() / 3600)

        return DoraMetrics(
            deployment_frequency=total_deploys / days,
            lead_time_hours=sum(lead_times) / len(lead_times) if lead_times else 0.0,
            change_failure_rate=failed / total_deploys if total_deploys > 0 else 0.0,
            mttr_hours=sum(mttr_values) / len(mttr_values) if mttr_values else 0.0,
        )
