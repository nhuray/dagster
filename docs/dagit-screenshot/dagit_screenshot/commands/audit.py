# pylint: disable=print-call

import os
import sys
from typing import Optional

from typing_extensions import TypedDict

from dagit_screenshot.utils import (
    ScreenshotSpec,
    load_spec_db,
    normalize_output_path,
    normalize_workspace_path,
    spec_id_to_relative_path,
)


class SpecAuditResult(TypedDict):
    id: str
    success: bool
    error: Optional[str]


def audit(
    spec_db_path: str, output_root: str, workspace_root: str, *, verify_outputs: bool = False
) -> None:
    spec_db = load_spec_db(spec_db_path)

    print(f"spec_db: {spec_db_path}")
    print(f"output_root: {output_root}")
    print(f"workspace_root: {workspace_root}")

    results = [
        _validate_spec(spec, output_root, workspace_root, verify_outputs) for spec in spec_db
    ]

    error_results = [r for r in results if not r["success"]]
    if len(error_results) == 0:
        print("No errors.")
    else:
        print(f"{len(error_results)} errors:")
        for result in error_results:
            print(f'  {result["id"]}: {result["error"]}')
        sys.exit(1)


def _validate_spec(
    spec: ScreenshotSpec, output_root: str, workspace_root: str, verify_outputs: bool
) -> SpecAuditResult:

    error: Optional[str] = None

    try:
        if spec["base_url"].startswith("http://localhost"):
            if 'workspace' not in spec:
                raise Exception("No workspace defined. Workspace required for local dagit.")
            workspace = spec["workspace"]
            workspace_path = normalize_workspace_path(workspace, workspace_root)
            if not os.path.exists(workspace_path):
                raise Exception(f"No workspace-defining file exists at {workspace_path}.")

        if verify_outputs:
            output_path = normalize_output_path(
                spec_id_to_relative_path(spec["id"]), output_root
            )
            if not os.path.exists(output_path):
                raise Exception(f"No screenshot image file exists at {output_path}.")

    except Exception as e:
        error = repr(e)

    success = error is None
    return {"id": spec["id"], "success": success, "error": error}
