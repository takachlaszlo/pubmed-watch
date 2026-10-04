"""Records stored in the canonical database and handed to the report, the API and n8n."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Article:
    pmid: str
    title: str
    abstract: list[tuple[str, str]] = field(default_factory=list)  # (label, text); label may be ""
    authors: list[str] = field(default_factory=list)
    journal: str = ""  # full journal title
    journal_abbrev: str = ""  # NLM abbreviation (MedlineTA)
    pub_year: str = ""
    pub_date: str = ""  # electronic or print publication date, YYYY-MM-DD / YYYY-MM / YYYY
    entrez_date: str = ""  # date the record entered PubMed (YYYY-MM-DD)
    pub_types: list[str] = field(default_factory=list)
    mesh: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    language: str = ""
    doi: str = ""
    pmcid: str = ""
    publication_status: str = ""  # aheadofprint | epublish | ppublish ...
    # derived
    kind: str = "other"  # guideline | systematic_review | protocol | rct | review | other
    is_update: bool = False
    # links (filled by the open-access lookup)
    url_fulltext: str = ""
    url_pdf: str = ""
    pdf_source: str = ""  # europepmc | unpaywall
    oa: bool = False

    @property
    def url_pubmed(self) -> str:
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"

    @property
    def url_doi(self) -> str:
        return f"https://doi.org/{self.doi}" if self.doi else ""

    @property
    def abstract_text(self) -> str:
        return "\n".join(f"{label}: {text}" if label else text for label, text in self.abstract)


@dataclass
class Trial:
    nct_id: str
    title: str
    official_title: str = ""
    status: str = ""
    study_type: str = ""
    phases: list[str] = field(default_factory=list)
    sponsor: str = ""
    countries: list[str] = field(default_factory=list)
    enrollment: int | None = None
    conditions: list[str] = field(default_factory=list)
    interventions: list[str] = field(default_factory=list)
    min_age: str = ""
    max_age: str = ""
    start_date: str = ""
    primary_completion: str = ""
    first_posted: str = ""
    last_update: str = ""
    summary: str = ""

    @property
    def url(self) -> str:
        return f"https://clinicaltrials.gov/study/{self.nct_id}"
