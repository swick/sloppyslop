"""Data classes for code review functionality."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml


class ReviewStore:
    """Manages storage and retrieval of reviews."""

    REVIEWS_DIR = ".llm-sandbox/reviews"

    def __init__(self, project_dir: Path):
        """Initialize the review store.

        Args:
            project_dir: Project root directory
        """
        self.project_dir = project_dir
        self.reviews_dir = project_dir / self.REVIEWS_DIR

    def list_ids(self) -> List[str]:
        """List all available review IDs.

        Returns:
            List of review IDs (sorted)
        """
        if not self.reviews_dir.exists():
            return []

        return sorted([f.stem for f in self.reviews_dir.glob("*.yaml")])

    def exists(self, review_id: str) -> bool:
        """Check if a review exists.

        Args:
            review_id: Review ID to check

        Returns:
            True if review exists, False otherwise
        """
        return (self.reviews_dir / f"{review_id}.yaml").exists()

    def load(self, review_id: str) -> "Review":
        """Load a review by ID.

        Args:
            review_id: Review ID to load

        Returns:
            Review object

        Raises:
            FileNotFoundError: If review doesn't exist
        """
        review_file = self.reviews_dir / f"{review_id}.yaml"
        if not review_file.exists():
            raise FileNotFoundError(f"Review '{review_id}' not found")

        return Review.from_yaml(review_file.read_text())

    def save(self, review_id: str, review: "Review") -> Path:
        """Save a review.

        Args:
            review_id: Review ID
            review: Review object to save

        Returns:
            Path to saved file
        """
        # Create directory if needed
        self.reviews_dir.mkdir(parents=True, exist_ok=True)

        # Write YAML
        review_file = self.reviews_dir / f"{review_id}.yaml"
        yaml_content = review.to_yaml()
        review_file.write_text(yaml_content)

        return review_file

    def remove(self, review_id: str) -> None:
        """Remove a review by ID.

        Args:
            review_id: Review ID to remove

        Raises:
            FileNotFoundError: If review doesn't exist
        """
        review_file = self.reviews_dir / f"{review_id}.yaml"
        if not review_file.exists():
            raise FileNotFoundError(f"Review '{review_id}' not found")

        review_file.unlink()


@dataclass
class SpawnedAgent:
    """Represents a sub-agent that was spawned during review."""

    agent_id: str
    task_description: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "agent_id": self.agent_id,
            "task_description": self.task_description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SpawnedAgent":
        """Create from dictionary."""
        return cls(
            agent_id=data["agent_id"],
            task_description=data["task_description"],
        )


@dataclass
class FindingsStatistics:
    """Statistics about review findings."""

    total_findings: int
    duplicates_count: Optional[int] = None
    unique_findings: Optional[int] = None
    by_category: Optional[Dict[str, int]] = None
    by_severity: Optional[Dict[str, int]] = None
    high_confidence_count: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {"total_findings": self.total_findings}
        if self.duplicates_count is not None:
            result["duplicates_count"] = self.duplicates_count
        if self.unique_findings is not None:
            result["unique_findings"] = self.unique_findings
        if self.by_category is not None:
            result["by_category"] = self.by_category
        if self.by_severity is not None:
            result["by_severity"] = self.by_severity
        if self.high_confidence_count is not None:
            result["high_confidence_count"] = self.high_confidence_count
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FindingsStatistics":
        """Create from dictionary."""
        return cls(
            total_findings=data["total_findings"],
            duplicates_count=data.get("duplicates_count"),
            unique_findings=data.get("unique_findings"),
            by_category=data.get("by_category"),
            by_severity=data.get("by_severity"),
            high_confidence_count=data.get("high_confidence_count"),
        )


@dataclass
class ReviewMetadata:
    """Metadata from the review agent's execution."""

    review_summary: str
    documentation_found: List[str]
    review_criteria_summary: str
    sub_agents_spawned: List[SpawnedAgent]
    findings_statistics: FindingsStatistics
    overall_assessment: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "review_summary": self.review_summary,
            "documentation_found": self.documentation_found,
            "review_criteria_summary": self.review_criteria_summary,
            "sub_agents_spawned": [a.to_dict() for a in self.sub_agents_spawned],
            "findings_statistics": self.findings_statistics.to_dict(),
            "overall_assessment": self.overall_assessment,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewMetadata":
        """Create from dictionary."""
        return cls(
            review_summary=data["review_summary"],
            documentation_found=data["documentation_found"],
            review_criteria_summary=data["review_criteria_summary"],
            sub_agents_spawned=[SpawnedAgent.from_dict(a) for a in data["sub_agents_spawned"]],
            findings_statistics=FindingsStatistics.from_dict(data["findings_statistics"]),
            overall_assessment=data["overall_assessment"],
        )


@dataclass
class FeedbackItem:
    """Represents a single review feedback item."""

    # Location information (required)
    file: str
    line_start: int
    line_end: int

    # Content (required)
    reason: str
    category: Literal["bug", "performance", "security", "style", "refactor", "documentation", "best-practice"]

    # Commit information (optional)
    commit: str = ""

    # Suggested code (optional)
    suggested_code: str = ""  # Replacement for lines line_start through line_end (inclusive)

    # Severity (optional, default: medium)
    severity: Literal["critical", "high", "medium", "low", "info"] = "medium"

    # Confidence/validation (optional, added by orchestrator)
    probability: Optional[float] = None
    probability_reasoning: str = ""

    # Duplicate tracking (optional, added by orchestrator)
    duplicate_of: Optional[int] = None
    duplicate_reasoning: str = ""

    # User override (optional, set during editing)
    ignore: bool = False

    # Stable ID (optional, generated on first access if not set)
    id: Optional[str] = None

    def get_short_id(self) -> str:
        """Get or generate a short unique ID for this feedback item.

        Returns:
            6-character hex string, stable across edits
        """
        if self.id is None:
            import hashlib

            # Create a stable hash from key attributes
            content = f"{self.file}:{self.line_start}:{self.line_end}:{self.commit}"
            hash_obj = hashlib.sha256(content.encode())
            self.id = hash_obj.hexdigest()[:6]

        return self.id

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "id": self.get_short_id(),  # Short unique ID for this suggestion
            "file": self.file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "reason": self.reason,
            "category": self.category,
            "commit": self.commit,  # Always include commit (now required)
        }

        # Add optional fields only if they have values
        if self.suggested_code:
            result["suggested_code"] = self.suggested_code
        if self.severity != "medium":
            result["severity"] = self.severity
        if self.probability is not None:
            result["probability"] = self.probability
        if self.probability_reasoning:
            result["probability_reasoning"] = self.probability_reasoning
        if self.duplicate_of is not None:
            result["duplicate_of"] = self.duplicate_of
        if self.duplicate_reasoning:
            result["duplicate_reasoning"] = self.duplicate_reasoning
        if self.ignore:
            result["ignore"] = self.ignore

        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeedbackItem":
        """Create from dictionary."""
        return cls(
            file=data["file"],
            line_start=data["line_start"],
            line_end=data["line_end"],
            reason=data["reason"],
            category=data["category"],
            commit=data.get("commit", ""),
            suggested_code=data.get("suggested_code", ""),
            severity=data.get("severity", "medium"),
            probability=data.get("probability"),
            probability_reasoning=data.get("probability_reasoning", ""),
            duplicate_of=data.get("duplicate_of"),
            duplicate_reasoning=data.get("duplicate_reasoning", ""),
            ignore=data.get("ignore", False),
            id=data.get("id"),  # Load stable ID if present
        )


@dataclass
class Review:
    """Container for code review results."""

    summary: Optional[str]  # Review summary text
    feedback: List[FeedbackItem]  # List of feedback items
    base_ref: str = ""  # Base commit/branch reference
    head_ref: str = ""  # Head commit/branch reference
    target_info: Dict[str, Any] = None  # Target info with 'type' key (github_pr, local, etc.)
    metadata: Optional[ReviewMetadata] = None  # Agent result metadata

    def __post_init__(self):
        """Initialize mutable defaults."""
        if self.target_info is None:
            self.target_info = {}

    def filter_feedback(self, probability_threshold: float = 0.5) -> List[FeedbackItem]:
        """Filter feedback by probability and exclude duplicates."""
        # Filter: keep items with probability >= threshold AND not marked as duplicate
        filtered = [
            f for f in self.feedback
            if (f.probability is None or f.probability >= probability_threshold)
            and f.duplicate_of is None
            and not f.ignore
        ]

        # Sort by probability (highest first, None values last)
        return sorted(filtered, key=lambda x: x.probability if x.probability is not None else 0.0, reverse=True)

    def get_statistics(self) -> Dict[str, int]:
        """Get review statistics."""
        duplicates = len([f for f in self.feedback if f.duplicate_of is not None])
        ignored = len([f for f in self.feedback if f.ignore])
        return {
            "total": len(self.feedback),
            "duplicates": duplicates,
            "ignored": ignored,
            "unique": len(self.feedback) - duplicates - ignored,
        }

    def to_yaml(self) -> str:
        """Serialize review to YAML format."""
        docs = []

        # Review info (base_ref, head_ref, target info)
        review_info = {}
        if self.base_ref:
            review_info["base_ref"] = self.base_ref
        if self.head_ref:
            review_info["head_ref"] = self.head_ref
        if self.target_info:
            review_info["target_info"] = self.target_info
        if review_info:
            docs.append({"review_info": review_info})

        # Summary
        if self.summary:
            docs.append({"summary": self.summary})

        # Metadata
        if self.metadata:
            docs.append({"metadata": self.metadata.to_dict()})

        # Feedback items
        for item in self.feedback:
            docs.append(item.to_dict())

        # Serialize all documents
        return yaml.dump_all(docs, default_flow_style=False, allow_unicode=True, sort_keys=False, width=100)

    @classmethod
    def from_yaml(cls, text: str) -> "Review":
        """Deserialize review from YAML format."""
        documents = list(yaml.safe_load_all(text))

        summary = None
        metadata = None
        feedback = []
        base_ref = ""
        head_ref = ""
        target_info = {}

        for doc in documents:
            if doc is None or not isinstance(doc, dict):
                continue

            # Check if this is review info
            if 'review_info' in doc and len(doc) == 1:
                review_info = doc['review_info']
                base_ref = review_info.get('base_ref', '')
                head_ref = review_info.get('head_ref', '')
                target_info = review_info.get('target_info', {})
            # Check if this is summary
            elif 'summary' in doc and len(doc) == 1:
                summary = doc['summary']
            # Check if this is metadata
            elif 'metadata' in doc and len(doc) == 1:
                metadata = ReviewMetadata.from_dict(doc['metadata'])
            # Otherwise it's a feedback item
            else:
                feedback.append(FeedbackItem.from_dict(doc))

        return cls(
            summary=summary,
            feedback=feedback,
            base_ref=base_ref,
            head_ref=head_ref,
            target_info=target_info,
            metadata=metadata
        )
