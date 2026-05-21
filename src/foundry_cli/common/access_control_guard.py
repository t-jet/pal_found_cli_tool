#!/usr/bin/env python3
"""AccessControlGuard — 8-step precedence model (SRS §4.2, ADR-007).

Implements the access control evaluation order:
1. Operation-level ENABLED
2. Namespace-level ENABLED
3. Operation-level READONLY override (false = permit write when parent READONLY=true)
4. Namespace-level READONLY override (false = permit write when parent READONLY=true)
5. Global READONLY (blocks writes)
6. Namespace-level METADATA_ONLY override (false = permit content when parent METADATA_ONLY=true)
7. Global METADATA_ONLY (blocks content reads not in allow-list)
8. Permit

Per ADR-007, operation-level READONLY=true is NOT independently supported.
Only READONLY=false override of parent READONLY=true is valid.
"""

import logging
import os
from pathlib import Path
from typing import Optional, Set

from foundry_cli.common.config_loader import ConfigLoader
from foundry_cli.common.error_serializer import EXIT_ACCESS_CONTROL


logger = logging.getLogger(__name__)


# Write operations — heuristic: operations whose name contains known write verbs
# are classified as writes; all others are reads.
_WRITE_VERBS = frozenset({
    "create", "delete", "put", "post", "patch", "replace", "update",
    "add", "remove", "commit", "abort", "upload", "download",
    "cancel", "revoke", "preregister", "execute", "apply",
    "blocking_continue", "streaming_continue", "rag_context",
})

# Read-only metadata operations that are permitted even in METADATA_ONLY tier
# when NOT in the allow-list file (these are the minimal structural metadata)
_METADATA_VERBS = frozenset({
    "get", "list", "search",
})


class AccessControlError(Exception):
    """Access control policy violation (exit code 8).

    Attributes
    ----------
    message : str
        Human-readable error message.
    exit_code : int
        Exit code (always EXIT_ACCESS_CONTROL = 8).
    step : int
        Which step of the 8-step precedence model triggered the block.
    """

    def __init__(self, message: str, step: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = EXIT_ACCESS_CONTROL
        self.step = step


class AccessControlGuard:
    """Evaluates access control using 8-step precedence model (SRS §4.2).

    Parameters
    ----------
    cfg : ConfigLoader
        Configuration instance.
    namespace : str
        Namespace name (e.g., "DATASETS").
    metadata_allowlist_path : str, optional
        Path to metadata allow-list file; if None, uses the canonical location.

    Usage
    -----
    >>> guard = AccessControlGuard(cfg, "DATASETS")
    >>> guard.check("dataset", "create")  # raises AccessControlError on block
    """

    def __init__(
        self,
        cfg: ConfigLoader,
        namespace: str,
        metadata_allowlist_path: Optional[str] = None,
    ) -> None:
        self.cfg = cfg
        self.namespace = namespace.upper()
        self._metadata_allowlist: Optional[Set[str]] = None
        self._metadata_allowlist_path = metadata_allowlist_path

    def _is_write_operation(self, operation: str) -> bool:
        """Classify an operation as a write operation.

        Heuristic: if the operation name (lowercase) starts with any known
        write verb, it's classified as a write.  This covers the 355 SDK
        operations without hardcoding a per-namespace list.

        Parameters
        ----------
        operation : str
            Operation name (e.g., "create", "get", "put_schema").

        Returns
        -------
        bool
            True if the operation is classified as a write.
        """
        op_lower = operation.lower()
        for verb in _WRITE_VERBS:
            if op_lower == verb or op_lower.startswith(verb + "_"):
                return True
        return False

    def _get_namespace_readonly(self) -> bool:
        """Check if namespace-level READONLY is explicitly set to true.

        Returns
        -------
        bool
            True if FOUNDRY_AGENTIC_CLI_{NS}_READONLY is explicitly "true".
        """
        ns_readonly_env = f"FOUNDRY_AGENTIC_CLI_{self.namespace}_READONLY"
        ns_readonly_val = os.environ.get(ns_readonly_env)
        if ns_readonly_val is None:
            return False
        return ns_readonly_val.lower() in ("true", "1", "yes")

    def _get_namespace_metadata_only(self) -> bool:
        """Check if namespace-level METADATA_ONLY is explicitly set to true.

        Returns
        -------
        bool
            True if FOUNDRY_AGENTIC_CLI_{NS}_METADATA_ONLY is explicitly "true".
        """
        ns_metadata_env = f"FOUNDRY_AGENTIC_CLI_{self.namespace}_METADATA_ONLY"
        ns_metadata_val = os.environ.get(ns_metadata_env)
        if ns_metadata_val is None:
            return False
        return ns_metadata_val.lower() in ("true", "1", "yes")

    def _log_decision(
        self,
        decision: str,
        resource: str,
        operation: str,
        step: int,
        reason: str,
    ) -> None:
        """Log access control decision as NDJSON (ADR-005).

        Parameters
        ----------
        decision : str
            "BLOCKED" or "PERMITTED".
        resource : str
            Resource name.
        operation : str
            Operation name.
        step : int
            Step number (1-8) in precedence model.
        reason : str
            Human-readable reason for the decision.
        """
        logger.info(
            "Access control decision: %s (Step %d) — %s",
            decision,
            step,
            reason,
            extra={
                "access_decision": decision,
                "op": f"{self.namespace.lower()}.{resource.lower()}.{operation.lower()}",
                "step": step,
                "reason": reason,
            },
        )

    def _load_metadata_allowlist(self) -> Set[str]:
        """Load metadata allow-list from canonical file.

        Returns
        -------
        set of str
            Set of operation keys (e.g., "datasets.dataset.get") permitted
            in METADATA_ONLY tier.

        Notes
        -----
        The allow-list file is a Markdown table.  This parser is intentionally
        lightweight and extracts lines containing "PERMITTED" to build the set.
        """
        allowlist_path: Optional[Path]
        if self._metadata_allowlist_path:
            allowlist_path = Path(self._metadata_allowlist_path)
        else:
            # Default: look for metadata-allow-list.md in .ept/docs
            allowlist_path = Path(".ept/docs/deliverables/architecture/metadata-allow-list.md")

        if not allowlist_path.exists():
            return set()

        permitted: Set[str] = set()
        text = allowlist_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "| PERMITTED" not in line:
                continue
            # Extract SDK path (first column after |)
            parts = [p.strip() for p in line.strip().split("|")]
            if len(parts) >= 2:
                sdk_path = parts[1].strip()
                permitted.add(sdk_path.lower())
        return permitted

    def _is_in_metadata_allowlist(self, resource: str, operation: str) -> bool:
        """Check if an operation is in the metadata allow-list.

        Parameters
        ----------
        resource : str
            Resource name.
        operation : str
            Operation name.

        Returns
        -------
        bool
            True if the operation is permitted in METADATA_ONLY tier.
        """
        if self._metadata_allowlist is None:
            self._metadata_allowlist = self._load_metadata_allowlist()

        ns_lower = self.namespace.lower()
        key = f"{ns_lower}.{resource.lower()}.{operation.lower()}"
        return key in self._metadata_allowlist

    def check(
        self,
        resource: str,
        operation: str,
    ) -> None:
        """Evaluate access control for an operation (8-step precedence).

        Parameters
        ----------
        resource : str
            Resource/class name (e.g., "dataset", "branch").
        operation : str
            Operation name (e.g., "create", "get").

        Raises
        ------
        AccessControlError
            If access control blocks the operation.
        """
        resource_upper = resource.upper()
        op_upper = operation.upper()
        full_key = f"{self.namespace}_{resource_upper}_{op_upper}"
        is_write = self._is_write_operation(operation)

        # Step 1: Operation-level ENABLED
        enabled_env = f"FOUNDRY_AGENTIC_CLI_{full_key}_ENABLED"
        enabled_val = os.environ.get(enabled_env)
        if enabled_val is not None and enabled_val.lower() == "false":
            self._log_decision("BLOCKED", resource, operation, 1,
                               f"ENABLED=false for {full_key}")
            raise AccessControlError(
                f"Operation blocked: ENABLED=false for {full_key}", step=1
            )

        # Step 2: Namespace-level ENABLED
        ns_enabled_env = f"FOUNDRY_AGENTIC_CLI_{self.namespace}_ENABLED"
        ns_enabled_val = os.environ.get(ns_enabled_env)
        if ns_enabled_val is not None and ns_enabled_val.lower() == "false":
            self._log_decision("BLOCKED", resource, operation, 2,
                               f"ENABLED=false for {self.namespace}")
            raise AccessControlError(
                f"Namespace blocked: ENABLED=false for {self.namespace}", step=2
            )

        # Steps 3-5: READONLY evaluation (only for write operations)
        if is_write:
            parent_readonly = (
                self.cfg.global_readonly or self._get_namespace_readonly()
            )

            if parent_readonly:
                # Step 3: Operation-level READONLY override
                op_readonly_env = f"FOUNDRY_AGENTIC_CLI_{full_key}_READONLY"
                op_readonly_val = os.environ.get(op_readonly_env)
                if op_readonly_val is not None and op_readonly_val.lower() == "false":
                    self._log_decision(
                        "PERMITTED", resource, operation, 3,
                        f"READONLY=false override for {full_key}"
                    )
                    return  # Explicit write permit for this operation

                # Step 4: Namespace-level READONLY override
                ns_readonly_env = f"FOUNDRY_AGENTIC_CLI_{self.namespace}_READONLY"
                ns_readonly_val = os.environ.get(ns_readonly_env)
                if ns_readonly_val is not None and ns_readonly_val.lower() == "false":
                    self._log_decision(
                        "PERMITTED", resource, operation, 4,
                        f"READONLY=false override for {self.namespace}"
                    )
                    return  # Explicit write permit for this namespace

            # Step 5: Global READONLY
            if self.cfg.global_readonly:
                self._log_decision(
                    "BLOCKED", resource, operation, 5,
                    "Global READONLY=true blocks write"
                )
                raise AccessControlError(
                    "Operation blocked: read-only mode active", step=5
                )

        # Steps 6-7: METADATA_ONLY evaluation
        parent_metadata_only = (
            self.cfg.global_metadata_only or self._get_namespace_metadata_only()
        )

        if parent_metadata_only:
            # Step 6: Namespace-level METADATA_ONLY override
            ns_metadata_env = f"FOUNDRY_AGENTIC_CLI_{self.namespace}_METADATA_ONLY"
            ns_metadata_val = os.environ.get(ns_metadata_env)
            if ns_metadata_val is not None and ns_metadata_val.lower() == "false":
                self._log_decision(
                    "PERMITTED", resource, operation, 6,
                    f"METADATA_ONLY=false override for {self.namespace}"
                )
                return  # Explicit metadata override

        # Step 7: Global METADATA_ONLY — block content reads not in allow-list
        if self.cfg.global_metadata_only:
            # METADATA_ONLY implies READONLY (FR-ACL-4), writes already blocked above
            if not is_write and not self._is_in_metadata_allowlist(resource, operation):
                self._log_decision(
                    "BLOCKED", resource, operation, 7,
                    "Operation not in metadata allow-list"
                )
                raise AccessControlError(
                    "Operation blocked: not in metadata allow-list", step=7
                )

        # Step 8: Permit
        self._log_decision(
            "PERMITTED", resource, operation, 8, "Default full access"
        )
        return

    def _is_metadata_operation(self, resource: str, operation: str) -> bool:
        """Check if an operation is a structural metadata operation.

        Heuristic: operations whose name starts with known metadata verbs
        (get, list, search) are structural metadata and are always permitted
        in METADATA_ONLY tier even if not explicitly in the allow-list.

        Parameters
        ----------
        resource : str
            Resource name.
        operation : str
            Operation name.

        Returns
        -------
        bool
            True if the operation is structural metadata.
        """
        op_lower = operation.lower()
        for verb in _METADATA_VERBS:
            if op_lower == verb or op_lower.startswith(verb + "_"):
                return True
        return False
