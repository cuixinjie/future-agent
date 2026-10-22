"""Explicit local UTF-8 material import; never fetches the supplied source URL."""

from pathlib import Path

from future_agent.domain.evidence import EvidenceImport


class JsonMaterialImporter:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> EvidenceImport:
        with self.path.open(encoding="utf-8", newline="") as stream:
            return EvidenceImport.model_validate_json(stream.read())
