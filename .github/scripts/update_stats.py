from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


# ============================================================
# README CONFIGURATION
# ============================================================

README_PATH = Path(os.getenv("README_PATH", "README.md"))

START_MARKER = os.getenv(
    "STATS_START_MARKER",
    "<!-- STATS_START -->",
)

END_MARKER = os.getenv(
    "STATS_END_MARKER",
    "<!-- STATS_END -->",
)


# ============================================================
# LANGUAGE DETECTION
# ============================================================
#
# Language percentages are based on lines YOU added in commits
# authored by one of your configured email addresses.
#
# This is intentionally NOT based on the current contents of
# entire repositories.
#
# That means contributing 200 lines to a huge Java repository
# only counts your 200 lines, not the whole Java codebase.
# ============================================================

LANGUAGE_BY_EXTENSION = {
    # TypeScript / JavaScript
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",

    # Python
    ".py": "Python",

    # Java
    ".java": "Java",

    # OCaml
    ".ml": "OCaml",
    ".mli": "OCaml",

    # C / C++
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".hh": "C++",

    # C#
    ".cs": "C#",

    # Other common languages
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".scala": "Scala",

    # Web
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "Sass",
    ".less": "Less",

    # Data / database
    ".sql": "SQL",

    # Shell
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".fish": "Shell",

    # Misc.
    ".r": "R",
    ".dart": "Dart",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hrl": "Erlang",
    ".lua": "Lua",
}


# Don't let dependency lockfiles / generated stuff make the
# "lines written" language calculation weird.
IGNORED_BASENAMES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
    "poetry.lock",
    "pipfile.lock",
    "cargo.lock",
    "composer.lock",
    "gemfile.lock",
}

IGNORED_PATH_PARTS = {
    "node_modules",
    "vendor",
    "dist",
    "build",
    ".next",
    "coverage",
    "target",
    "__pycache__",
}


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass(frozen=True)
class Source:
    name: str
    host: str
    api_base: str
    username: str
    token: str


@dataclass
class Totals:
    commits: int = 0
    additions: int = 0
    deletions: int = 0
    repositories: int = 0


# ============================================================
# CONFIG HELPERS
# ============================================================

def required_env(name: str) -> str:
    """
    Read a required environment variable.

    Also catches the FILL_THIS_IN placeholders so the workflow
    fails clearly instead of silently producing bad statistics.
    """

    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required configuration: {name}"
        )

    if "FILL_THIS_IN" in value:
        raise RuntimeError(
            f"You still need to fill in: {name}"
        )

    return value


def parse_author_emails() -> set[str]:
    """
    Parse AUTHOR_EMAILS from the multi-line YAML environment
    variable.
    """

    raw = required_env("AUTHOR_EMAILS")

    emails = {
        line.strip().lower()
        for line in raw.splitlines()
        if line.strip()
        and not line.strip().startswith("#")
    }

    if not emails:
        raise RuntimeError(
            "AUTHOR_EMAILS must contain at least one email."
        )

    return emails


# ============================================================
# GITHUB API
# ============================================================

def api_get(
    source: Source,
    path: str,
    params: dict[str, str | int] | None = None,
):
    """
    Perform an authenticated REST API request against either:

        api.github.com

    or:

        github.coecis.cornell.edu/api/v3
    """

    url = f"{source.api_base.rstrip('/')}/{path.lstrip('/')}"

    if params:
        url += "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {source.token}",
            "User-Agent": "profile-stats-action",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=60,
        ) as response:
            return json.load(response)

    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"{source.name}: GitHub API returned HTTP "
            f"{exc.code}. Check the token and its permissions."
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"{source.name}: could not reach {source.host}. "
            "If this is Cornell, the GitHub-hosted runner may "
            "not have network access to the Enterprise server."
        ) from exc


def validate_source(source: Source) -> None:
    """
    Confirm that each token actually belongs to the username
    configured for that GitHub installation.
    """

    user = api_get(source, "/user")

    actual_username = str(
        user.get("login", "")
    )

    if actual_username.lower() != source.username.lower():
        raise RuntimeError(
            f"{source.name}: token belongs to "
            f"'{actual_username}', but '{source.username}' "
            "was configured."
        )


def list_repositories(source: Source) -> list[dict]:
    """
    Return repositories visible to the authenticated account:

      - owned repositories
      - collaborative repositories
      - repositories available through org membership

    Visibility depends on the token's permissions.
    """

    repositories: list[dict] = []

    page = 1

    while True:
        batch = api_get(
            source,
            "/user/repos",
            {
                "per_page": 100,
                "page": page,
                "affiliation": (
                    "owner,"
                    "collaborator,"
                    "organization_member"
                ),
                "sort": "full_name",
            },
        )

        if not isinstance(batch, list):
            raise RuntimeError(
                f"{source.name}: unexpected response from "
                "repositories API."
            )

        repositories.extend(batch)

        if len(batch) < 100:
            break

        page += 1

    return repositories


# ============================================================
# SAFE GIT AUTHENTICATION
# ============================================================

def create_askpass_script(directory: Path) -> Path:
    """
    Create a temporary Git credential helper.

    This avoids embedding PATs inside clone URLs.
    """

    path = directory / "git-askpass.sh"

    path.write_text(
        """#!/bin/sh
case "$1" in
  *Username*)
    printf '%s\\n' "$GIT_AUTH_USERNAME"
    ;;
  *Password*)
    printf '%s\\n' "$GIT_AUTH_TOKEN"
    ;;
  *)
    exit 1
    ;;
esac
""",
        encoding="utf-8",
    )

    path.chmod(
        path.stat().st_mode | stat.S_IXUSR
    )

    return path


def git_environment(
    source: Source,
    askpass: Path,
) -> dict[str, str]:
    env = os.environ.copy()

    env.update(
        {
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTH_USERNAME": source.username,
            "GIT_AUTH_TOKEN": source.token,
        }
    )

    return env


# ============================================================
# GIT HELPERS
# ============================================================

def run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """
    Execute Git without automatically printing stdout/stderr.

    This is intentional: failed clone output could contain
    the name of a private repository.
    """

    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def clone_repository(
    source: Source,
    clone_url: str,
    destination: Path,
    askpass: Path,
) -> None:
    """
    Mirror-clone the complete repository history.

    We need complete history because the goal is lifetime
    authored additions/deletions, not only recent commits.
    """

    result = run_git(
        [
            "clone",
            "--mirror",
            "--quiet",
            clone_url,
            str(destination),
        ],
        env=git_environment(
            source,
            askpass,
        ),
    )

    if result.returncode != 0:
        # Deliberately don't print stderr because this workflow
        # may live in a public profile repository.
        raise RuntimeError(
            f"{source.name}: a repository could not be cloned."
        )


# ============================================================
# LANGUAGE HELPERS
# ============================================================

def should_ignore_path(path_text: str) -> bool:
    normalized = path_text.replace("\\", "/")

    path = Path(normalized)

    parts = {
        part.lower()
        for part in path.parts
    }

    basename = path.name.lower()

    if basename in IGNORED_BASENAMES:
        return True

    ignored_parts_lower = {
        part.lower()
        for part in IGNORED_PATH_PARTS
    }

    if parts & ignored_parts_lower:
        return True

    if basename.endswith(".min.js"):
        return True

    if basename.endswith(".min.css"):
        return True

    if basename.endswith(".map"):
        return True

    # Notebook JSON diffs are not meaningful "LOC" statistics.
    if basename.endswith(".ipynb"):
        return True

    return False


def language_for_path(
    path_text: str,
) -> str | None:

    if should_ignore_path(path_text):
        return None

    extension = Path(
        path_text
    ).suffix.lower()

    return LANGUAGE_BY_EXTENSION.get(
        extension
    )


# ============================================================
# COMMIT PROCESSING
# ============================================================

def repository_is_empty(
    repo_path: Path,
) -> bool:
    result = run_git(
        [
            "rev-list",
            "--all",
            "--count",
        ],
        cwd=repo_path,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Could not inspect repository history."
        )

    try:
        return int(
            result.stdout.strip() or "0"
        ) == 0
    except ValueError as exc:
        raise RuntimeError(
            "Unexpected Git history output."
        ) from exc


def process_repository(
    repo_path: Path,
    author_emails: set[str],
    seen_commits: set[str],
    totals: Totals,
    language_additions: Counter[str],
) -> bool:
    """
    Scan the full Git history.

    For commits authored by one of the configured email
    addresses:

      - count the commit
      - count added lines
      - count deleted lines
      - classify added lines by language

    Commit SHAs are globally deduplicated, preventing the same
    commit from being counted twice when it exists in multiple
    repositories/forks/mirrors.
    """

    if repository_is_empty(repo_path):
        return False

    result = run_git(
        [
            "log",
            "--all",
            "--numstat",
            "--format="
            "__PROFILE_STATS_COMMIT__"
            "%H%x09%ae",
        ],
        cwd=repo_path,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Could not inspect Git history."
        )

    repo_has_authored_commit = False
    count_current_commit = False

    for line in result.stdout.splitlines():

        # ----------------------------------------------------
        # New commit
        # ----------------------------------------------------
        if line.startswith(
            "__PROFILE_STATS_COMMIT__"
        ):
            payload = line.removeprefix(
                "__PROFILE_STATS_COMMIT__"
            )

            try:
                sha, email = payload.split(
                    "\t",
                    1,
                )
            except ValueError:
                count_current_commit = False
                continue

            email = email.strip().lower()

            is_yours = (
                email in author_emails
            )

            not_counted_before = (
                sha not in seen_commits
            )

            count_current_commit = (
                is_yours
                and not_counted_before
            )

            if count_current_commit:
                seen_commits.add(sha)

                totals.commits += 1

                repo_has_authored_commit = True

            continue


        # ----------------------------------------------------
        # File-change line belonging to current commit
        # ----------------------------------------------------
        if not count_current_commit:
            continue

        if not line:
            continue

        if "\t" not in line:
            continue

        parts = line.split(
            "\t",
            2,
        )

        if len(parts) != 3:
            continue

        (
            added_text,
            deleted_text,
            path_text,
        ) = parts


        # Git prints "-" for binary changes.
        if (
            added_text == "-"
            or deleted_text == "-"
        ):
            continue


        try:
            added = int(
                added_text
            )

            deleted = int(
                deleted_text
            )

        except ValueError:
            continue


        totals.additions += added
        totals.deletions += deleted


        # ----------------------------------------------------
        # Language statistics
        # ----------------------------------------------------
        language = language_for_path(
            path_text
        )

        if (
            language
            and added > 0
        ):
            language_additions[
                language
            ] += added


    return repo_has_authored_commit


# ============================================================
# README OUTPUT
# ============================================================

def render_stats(
    totals: Totals,
    languages: Counter[str],
) -> str:
    """
    Build the text block that gets inserted into README.md.
    """

    rule = (
        "--------------------------------------------------"
    )

    lines = [
        "```text",
        rule,
        "  GITHUB ACTIVITY",
        rule,

        (
            "  Authored Commits       "
            f"{totals.commits:>12,}"
        ),

        (
            "  Lines Added            "
            f"{totals.additions:>12,}"
        ),

        (
            "  Lines Deleted          "
            f"{totals.deletions:>12,}"
        ),

        (
            "  Repositories           "
            f"{totals.repositories:>12,}"
        ),
    ]


    total_language_lines = sum(
        languages.values()
    )


    if total_language_lines:
        lines.extend(
            [
                "",
                (
                    "  Top Languages "
                    "(by authored additions)"
                ),
            ]
        )


        # Show the top five languages.
        for (
            language,
            line_count,
        ) in languages.most_common(5):

            percentage = (
                line_count
                / total_language_lines
                * 100
            )

            lines.append(
                f"  {language:<18}"
                f"{percentage:>10.1f}%"
            )


    lines.extend(
        [
            rule,
            "```",
        ]
    )


    return "\n".join(lines)


def update_readme(
    stats_block: str,
) -> bool:
    """
    Replace ONLY the content between:

        <!-- STATS_START -->
        <!-- STATS_END -->

    Everything else in the README remains untouched.
    """

    if not README_PATH.exists():
        raise RuntimeError(
            f"{README_PATH} does not exist."
        )


    original = README_PATH.read_text(
        encoding="utf-8"
    )


    pattern = re.compile(
        re.escape(START_MARKER)
        + r".*?"
        + re.escape(END_MARKER),
        flags=re.DOTALL,
    )


    matches = pattern.findall(
        original
    )


    if len(matches) != 1:
        raise RuntimeError(
            f"{README_PATH} must contain exactly one "
            f"{START_MARKER} ... {END_MARKER} block."
        )


    replacement = (
        f"{START_MARKER}\n"
        f"{stats_block}\n"
        f"{END_MARKER}"
    )


    updated = pattern.sub(
        lambda _: replacement,
        original,
        count=1,
    )


    if updated == original:
        return False


    README_PATH.write_text(
        updated,
        encoding="utf-8",
    )


    return True


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    # --------------------------------------------------------
    # Your author identities
    # --------------------------------------------------------
    author_emails = (
        parse_author_emails()
    )


    # --------------------------------------------------------
    # GitHub installations
    # --------------------------------------------------------
    sources = [
        Source(
            name="Personal GitHub",

            host=required_env(
                "PERSONAL_GITHUB_HOST"
            ),

            api_base=required_env(
                "PERSONAL_GITHUB_API_BASE"
            ),

            username=required_env(
                "PERSONAL_GITHUB_USERNAME"
            ),

            token=required_env(
                "PERSONAL_STATS_TOKEN"
            ),
        ),

        Source(
            name="Cornell GitHub Enterprise",

            host=required_env(
                "CORNELL_GITHUB_HOST"
            ),

            api_base=required_env(
                "CORNELL_GITHUB_API_BASE"
            ),

            username=required_env(
                "CORNELL_GITHUB_USERNAME"
            ),

            token=required_env(
                "CORNELL_STATS_TOKEN"
            ),
        ),
    ]


    # --------------------------------------------------------
    # Combined statistics
    # --------------------------------------------------------
    totals = Totals()

    language_additions: Counter[str] = (
        Counter()
    )

    # One commit may exist in a fork/mirror on both servers.
    # Only count each SHA once.
    seen_commits: set[str] = set()


    # --------------------------------------------------------
    # Temporary working directory
    # --------------------------------------------------------
    with tempfile.TemporaryDirectory(
        prefix="profile-stats-"
    ) as temp_name:

        temp_dir = Path(
            temp_name
        )

        askpass = (
            create_askpass_script(
                temp_dir
            )
        )


        # ----------------------------------------------------
        # Process personal GitHub + Cornell Enterprise
        # ----------------------------------------------------
        for source in sources:

            print(
                f"Validating {source.name}..."
            )


            validate_source(
                source
            )


            repositories = (
                list_repositories(
                    source
                )
            )


            print(
                f"{source.name}: "
                f"{len(repositories)} "
                "accessible repositories found."
            )


            for index, repository in enumerate(
                repositories,
                start=1,
            ):

                # Ignore disabled repos.
                if repository.get(
                    "disabled",
                    False,
                ):
                    continue


                clone_url = repository.get(
                    "clone_url"
                )


                if not clone_url:
                    continue


                # Intentionally don't use the real repository
                # name in the local folder.
                #
                # Your profile repository may be public and
                # Actions logs may therefore be visible.
                repo_dir = (
                    temp_dir
                    / (
                        f"repo_"
                        f"{source.name.replace(' ', '_')}_"
                        f"{index}.git"
                    )
                )


                try:
                    clone_repository(
                        source,
                        str(clone_url),
                        repo_dir,
                        askpass,
                    )


                    contributed = (
                        process_repository(
                            repo_dir,
                            author_emails,
                            seen_commits,
                            totals,
                            language_additions,
                        )
                    )


                except RuntimeError as exc:
                    # Do NOT include the repo name or URL here.
                    raise RuntimeError(
                        f"{source.name}: failed while "
                        f"processing repository "
                        f"{index} of "
                        f"{len(repositories)}."
                    ) from exc


                if contributed:
                    totals.repositories += 1


    # --------------------------------------------------------
    # Generate README block
    # --------------------------------------------------------
    stats_block = render_stats(
        totals,
        language_additions,
    )


    changed = update_readme(
        stats_block
    )


    if changed:
        print(
            "README statistics updated."
        )
    else:
        print(
            "Statistics unchanged; README already current."
        )


if __name__ == "__main__":
    main()
