"""Persist setup choices and derive progress from the existing installation."""

import sys
import tempfile
import zipfile
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, model_validator

from chamberlain.connections.build import bridge_directory
from chamberlain.connections.load import load_connections, profile_path
from chamberlain.connections.model import (
    CalDavSource,
    Connections,
    GoogleCalendarSource,
    OwlCalendarSource,
    ThunderbirdContactsSource,
    ThunderbirdMailSource,
)
from chamberlain.connectors.caldav.credentials import SERVICE as CALDAV_SERVICE
from chamberlain.connectors.caldav.credentials import credential_name
from chamberlain.connectors.calendar_support.secrets import SecretStore, default_store
from chamberlain.connectors.google_calendar import auth
from chamberlain.connectors.thunderbird.client import BridgeClient, RuntimeConnector
from chamberlain.connectors.thunderbird.native_host import RUNTIME_FILE
from chamberlain.connectors.thunderbird.setup import addon_version
from chamberlain.dataroot import refuse_inside_checkout
from chamberlain.schema.base import Model
from chamberlain.schema.errors import ChamberlainError, ErrorCode


class SetupProvider(StrEnum):
    THUNDERBIRD = "thunderbird"
    GOOGLE = "google"
    ICLOUD = "icloud"


Account = Annotated[str, Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")]


class SetupSelection(Model):
    providers: tuple[SetupProvider, ...] = Field(min_length=1, max_length=3)
    google_account: Account | None = None
    apple_id: Account | None = None

    @model_validator(mode="after")
    def _selected_accounts(self) -> Self:
        if self.google_account is not None and SetupProvider.GOOGLE not in self.providers:
            raise ValueError("Select Google before naming its account.")
        if self.apple_id is not None and SetupProvider.ICLOUD not in self.providers:
            raise ValueError("Select iCloud before naming its account.")
        if len(set(self.providers)) != len(self.providers):
            raise ValueError("Select each provider once.")
        return self


class SetupStep(Model):
    id: str
    detail: str
    provider: SetupProvider | None = None
    command: str | None = None


class SetupStatus(Model):
    providers: tuple[SetupProvider, ...] = ()
    done: tuple[SetupStep, ...] = ()
    next: tuple[SetupStep, ...] = ()
    waiting: tuple[SetupStep, ...] = ()


def _selection_path(data_root: Path) -> Path:
    return profile_path(data_root).with_name("setup.json")


def setup_select(
    data_root: Path,
    providers: Sequence[SetupProvider],
    *,
    google_account: str | None = None,
    apple_id: str | None = None,
) -> SetupSelection:
    """Keep only the explicitly selected providers and optional account identifiers."""
    root = refuse_inside_checkout(data_root)
    try:
        selected = SetupSelection(
            providers=tuple(providers),
            google_account=google_account,
            apple_id=apple_id,
        )
    except ValidationError:
        raise ChamberlainError(
            ErrorCode.REQUEST_INVALID,
            "Choose each provider once and give email addresses only for selected calendars.",
        ) from None
    path = _selection_path(root)
    body = selected.model_dump_json(indent=2, exclude_none=True) + "\n"
    try:
        if path.is_file() and path.read_bytes() == body.encode("utf-8"):
            return selected
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=path.parent) as staging:
            pending = Path(staging) / path.name
            pending.write_bytes(body.encode("utf-8"))
            pending.replace(path)
    except OSError:
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "The setup choices could not be saved.",
            fix="Check that the data root's config directory is writable.",
        ) from None
    return selected


def _read_selection(data_root: Path) -> SetupSelection | None:
    try:
        return SetupSelection.model_validate_json(_selection_path(data_root).read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValidationError):
        raise ChamberlainError(
            ErrorCode.CONFIG_INVALID,
            "The saved setup choices cannot be read.",
            fix="Choose the providers again with chamberlain setup select.",
        ) from None


Phase = Literal["done", "next", "waiting"]


@dataclass
class _Progress:
    provider: SetupProvider
    steps: dict[Phase, list[SetupStep]]

    def add(self, phase: Phase, step: str, detail: str, command: str | None = None) -> None:
        self.steps[phase].append(
            SetupStep(
                id=f"{self.provider.value}.{step}",
                provider=self.provider,
                detail=detail,
                command=command,
            )
        )


def _thunderbird(root: Path, profile: Connections, progress: _Progress) -> None:
    bridge = bridge_directory(profile, root)
    current = addon_version()
    package = bridge / f"chamberlain-bridge-{current}.xpi"
    launcher = bridge / ("native-host.cmd" if sys.platform == "win32" else "native-host")
    running_version: str | None = None
    if (bridge / RUNTIME_FILE).is_file():
        with suppress(ChamberlainError):
            running_version = BridgeClient(RuntimeConnector(bridge)).hello().addon_version

    if running_version is not None:
        progress.add("done", "bridge", "The local native host and add-on answered the handshake.")
        if running_version != current:
            progress.add(
                "next", "update", "Update the installed add-on.", "chamberlain addon update"
            )
            return
    elif launcher.is_file() and zipfile.is_zipfile(package):
        progress.add("done", "package", "The current add-on package and host launcher are present.")
        progress.add(
            "waiting",
            "install",
            "Register the native manifest and install or start the add-on in Thunderbird.",
        )
        return
    else:
        progress.add(
            "next",
            "package",
            "Build the add-on and write its native host manifest.",
            "chamberlain setup thunderbird --manifest-dir <manifest-folder>",
        )
        return

    if any(
        source.enabled
        and isinstance(
            source, (ThunderbirdMailSource, ThunderbirdContactsSource, OwlCalendarSource)
        )
        for source in profile.sources.values()
    ):
        progress.add(
            "done", "binding", "At least one Thunderbird source is enabled in the profile."
        )
    else:
        progress.add(
            "waiting",
            "binding",
            "Choose a Thunderbird account and its folders or a calendar or address book to bind.",
        )


def _google_client(store: SecretStore) -> bool:
    try:
        client, secret = auth.load_client(store)
        return client.endswith(".apps.googleusercontent.com") and bool(secret.strip())
    except (KeyError, TypeError, ValueError):
        return False
    except ChamberlainError as error:
        if error.code is ErrorCode.SOURCE_NOT_CONFIGURED:
            return False
        raise


def _google_token(store: SecretStore, account: str) -> bool:
    try:
        return auth.stored_refresh_token(store, account) is not None
    except ChamberlainError as error:
        if error.code is ErrorCode.SOURCE_SIGN_IN_NEEDED:
            return False
        raise


def _google(
    profile: Connections, chosen: str | None, store: SecretStore, progress: _Progress
) -> None:
    if not _google_client(store):
        progress.add(
            "waiting",
            "client",
            "Have the owner supply the Google Desktop client in the terminal.",
            "chamberlain setup google client",
        )
        return
    progress.add("done", "client", "Google Desktop client credentials are stored.")
    account = chosen.strip().lower() if chosen is not None else None
    bindings = {
        entry.account.strip().lower()
        for entry in profile.sources.values()
        if isinstance(entry, GoogleCalendarSource) and entry.enabled
    }
    targets = {account} if account else bindings
    if not targets:
        linked = set(auth.accounts(store))
        if len(linked) != 1:
            progress.add("waiting", "account", "Choose the Google account to link and bind.")
            return
        targets = linked
    if not all(_google_token(store, target) for target in sorted(targets)):
        progress.add(
            "waiting",
            "consent",
            "Have the owner complete Google consent for the account to link.",
            "chamberlain setup google link <email>",
        )
        return
    progress.add("done", "consent", "The chosen Google accounts have saved sign-ins.")
    if targets <= bindings:
        progress.add(
            "done", "binding", "The chosen Google accounts have enabled calendar bindings."
        )
    else:
        progress.add(
            "waiting", "binding", "Choose a Google calendar and add its binding to the profile."
        )


def _icloud_source(source: CalDavSource) -> bool:
    try:
        host = urlsplit(source.url).hostname or ""
    except ValueError:
        return False
    return host == "caldav.icloud.com" or host.endswith("-caldav.icloud.com")


def _icloud(
    profile: Connections, chosen: str | None, store: SecretStore, progress: _Progress
) -> None:
    account = chosen.strip().lower() if chosen is not None else None
    bindings = [
        entry
        for entry in profile.sources.values()
        if isinstance(entry, CalDavSource)
        and entry.enabled
        and _icloud_source(entry)
        and (account is None or entry.username.strip().lower() == account)
    ]
    if not bindings and account is None:
        progress.add("waiting", "account", "Choose the Apple ID for the iCloud calendar.")
        return
    credentials = {entry.credential for entry in bindings}
    if account is not None and not credentials:
        credentials.add(credential_name(account))
    if not all(store.get(CALDAV_SERVICE, name) for name in sorted(credentials)):
        progress.add(
            "waiting",
            "credential",
            "Have the owner enter an app-specific password in the terminal.",
            "chamberlain setup icloud credential --apple-id <email>",
        )
        return
    progress.add("done", "credential", "The chosen iCloud calendar credentials are stored.")
    if bindings:
        progress.add("done", "binding", "The chosen iCloud calendars have enabled bindings.")
    else:
        progress.add(
            "waiting",
            "binding",
            "Choose and register the iCloud calendar.",
            "chamberlain setup icloud register --apple-id <email>",
        )


def setup_status(data_root: Path, *, store: SecretStore | None = None) -> SetupStatus:
    """Read observed setup state. No refresh, provider write, config edit or approval."""
    root = refuse_inside_checkout(data_root)
    selection = _read_selection(root)
    if selection is None:
        return SetupStatus(
            waiting=(
                SetupStep(
                    id="setup.selection",
                    detail="Choose which providers the owner wants to set up.",
                    command="chamberlain setup select <providers>",
                ),
            )
        )
    try:
        profile = load_connections(root)
    except ChamberlainError:
        return SetupStatus(
            providers=selection.providers,
            next=(
                SetupStep(
                    id="setup.profile",
                    detail="Correct the connection profile before resuming setup.",
                    command="chamberlain config validate",
                ),
            ),
        )
    steps: dict[Phase, list[SetupStep]] = {"done": [], "next": [], "waiting": []}
    for provider in selection.providers:
        progress = _Progress(provider, steps)
        if provider is SetupProvider.THUNDERBIRD:
            _thunderbird(root, profile, progress)
            continue
        try:
            credential_store = store if store is not None else default_store(root)
            if provider is SetupProvider.GOOGLE:
                _google(profile, selection.google_account, credential_store, progress)
            else:
                _icloud(profile, selection.apple_id, credential_store, progress)
        except (ChamberlainError, OSError, ValueError):
            progress.add(
                "waiting", "store", "The credential store could not be read. Check it and resume."
            )
    return SetupStatus(
        providers=selection.providers,
        done=tuple(steps["done"]),
        next=tuple(steps["next"]),
        waiting=tuple(steps["waiting"]),
    )
