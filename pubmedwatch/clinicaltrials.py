"""ClinicalTrials.gov API v2: newly registered, not yet closed paediatric trials."""
from __future__ import annotations

import logging
from datetime import date

from .config import TrialsConfig
from .http import HttpClient
from .models import Trial

log = logging.getLogger(__name__)

API = "https://clinicaltrials.gov/api/v2/studies"


def parse_study(study: dict) -> Trial:
    p = study.get("protocolSection", {})
    ident = p.get("identificationModule", {})
    status = p.get("statusModule", {})
    design = p.get("designModule", {})
    elig = p.get("eligibilityModule", {})
    locations = p.get("contactsLocationsModule", {}).get("locations", [])
    return Trial(
        nct_id=ident.get("nctId", ""),
        title=ident.get("briefTitle", ""),
        official_title=ident.get("officialTitle", ""),
        status=status.get("overallStatus", ""),
        study_type=design.get("studyType", ""),
        phases=[ph for ph in design.get("phases", []) if ph != "NA"],
        sponsor=p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", ""),
        countries=sorted({loc["country"] for loc in locations if loc.get("country")}),
        enrollment=design.get("enrollmentInfo", {}).get("count"),
        conditions=p.get("conditionsModule", {}).get("conditions", []),
        interventions=[i.get("name", "") for i in p.get("armsInterventionsModule", {}).get("interventions", [])],
        min_age=elig.get("minimumAge", ""),
        max_age=elig.get("maximumAge", ""),
        start_date=status.get("startDateStruct", {}).get("date", ""),
        primary_completion=status.get("primaryCompletionDateStruct", {}).get("date", ""),
        first_posted=status.get("studyFirstPostDateStruct", {}).get("date", ""),
        last_update=status.get("lastUpdatePostDateStruct", {}).get("date", ""),
        summary=" ".join(p.get("descriptionModule", {}).get("briefSummary", "").split()),
    )


def new_trials(http: HttpClient, cfg: TrialsConfig, since: date, until: date) -> list[Trial]:
    """Trials first posted between the two days (inclusive) that are still open."""
    parts = [f"AREA[StudyFirstPostDate]RANGE[{since.isoformat()}, {until.isoformat()}]"]
    if cfg.statuses:
        parts.append(f"AREA[OverallStatus]({' OR '.join(cfg.statuses)})")
    if cfg.max_age:
        parts.append(f"AREA[MaximumAge]RANGE[MIN, {cfg.max_age}]")
    advanced = " AND ".join(parts)
    found: dict[str, Trial] = {}
    for key, value in (("query.cond", cfg.conditions), ("query.term", cfg.terms)):
        if not value:
            continue
        params = {key: value, "filter.advanced": advanced, "pageSize": 100}
        while True:
            data = http.get_json(API, params)
            for study in data.get("studies", []):
                trial = parse_study(study)
                if trial.nct_id:
                    found[trial.nct_id] = trial
            token = data.get("nextPageToken")
            if not token:
                break
            params = {**params, "pageToken": token}
    return sorted(found.values(), key=lambda t: t.nct_id)
