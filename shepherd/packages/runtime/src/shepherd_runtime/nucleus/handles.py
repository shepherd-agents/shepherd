"""Pure substrate handle value nouns for the runtime nucleus."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import ClassVar, Literal, TypeAlias, cast

GIT_REPO_BASIS_SCHEMA = "shepherd.runtime.nucleus.gitrepo.basis.v1"
GIT_REPO_HANDLE_SCHEMA = "shepherd.runtime.nucleus.gitrepo.v1"

GitRepoAuthorityAtom: TypeAlias = Literal["read", "write"]
GitRepoAuthority: TypeAlias = frozenset[GitRepoAuthorityAtom]

_GIT_REPO_AUTHORITY_ATOMS: GitRepoAuthority = frozenset({"read", "write"})
_READ_AUTHORITY: GitRepoAuthority = frozenset({"read"})


@dataclass(frozen=True)
class GitRepoBasis:
    """Durable carrier-held basis for one Git-backed binding."""

    world_oid: str
    store_id: str
    resource_id: str
    head: str

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.world_oid, "GitRepo basis world_oid")
        _validate_non_empty_str(self.store_id, "GitRepo basis store_id")
        _validate_non_empty_str(self.resource_id, "GitRepo basis resource_id")
        _validate_non_empty_str(self.head, "GitRepo basis head")

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-shaped representation of this basis."""
        return {
            "schema": GIT_REPO_BASIS_SCHEMA,
            "world_oid": self.world_oid,
            "store_id": self.store_id,
            "resource_id": self.resource_id,
            "head": self.head,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> GitRepoBasis:
        """Rehydrate a GitRepo basis from its JSON-shaped representation."""
        if not isinstance(payload, Mapping):
            raise TypeError("GitRepo basis payload must be an object")
        if payload.get("schema") != GIT_REPO_BASIS_SCHEMA:
            raise ValueError("GitRepo basis payload schema is unsupported")
        world_oid = payload.get("world_oid")
        store_id = payload.get("store_id")
        resource_id = payload.get("resource_id")
        head = payload.get("head")
        if not isinstance(world_oid, str):
            raise TypeError("GitRepo basis payload world_oid must be a string")
        if not isinstance(store_id, str):
            raise TypeError("GitRepo basis payload store_id must be a string")
        if not isinstance(resource_id, str):
            raise TypeError("GitRepo basis payload resource_id must be a string")
        if not isinstance(head, str):
            raise TypeError("GitRepo basis payload head must be a string")
        return cls(world_oid=world_oid, store_id=store_id, resource_id=resource_id, head=head)


@dataclass(frozen=True)
class GitRepo:
    """Copyable value view of one Git-backed workspace binding.

    This is a pure value noun. It records the binding, basis, and advisory
    authority surface; it does not own custody and does not perform substrate
    operations.
    """

    # Handle-noun marker: schema/dispatch fences detect substrate handles by this
    # attribute so the deliberately import-isolated stacks (dialect step schema)
    # need no cross-package noun import. Future nouns (Folder, ...) carry it too;
    # the phase-ii `Handle` base subsumes it when it lands.
    __shepherd_handle_noun__: ClassVar[bool] = True

    binding: str
    basis: GitRepoBasis
    authority: GitRepoAuthority = _READ_AUTHORITY

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.binding, "GitRepo binding")
        if not isinstance(self.basis, GitRepoBasis):
            raise TypeError("GitRepo basis must be a GitRepoBasis")
        object.__setattr__(self, "authority", _coerce_git_repo_authority(self.authority))

    def readonly(self) -> GitRepo:
        """Return the read-only attenuation of this handle."""
        return self.allow_only(_READ_AUTHORITY)

    def allow_only(self, authority: Iterable[str]) -> GitRepo:
        """Return this handle attenuated to the provided authority atoms."""
        allowed = _coerce_git_repo_authority(authority)
        return GitRepo(binding=self.binding, basis=self.basis, authority=self.authority & allowed)

    def deny(self, authority: Iterable[str]) -> GitRepo:
        """Return this handle with the provided authority atoms removed."""
        denied = _coerce_git_repo_authority(authority)
        return GitRepo(binding=self.binding, basis=self.basis, authority=self.authority - denied)

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-shaped representation of this value noun."""
        return {
            "schema": GIT_REPO_HANDLE_SCHEMA,
            "binding": self.binding,
            "basis": self.basis.to_payload(),
            "authority": sorted(self.authority),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> GitRepo:
        """Rehydrate a GitRepo value noun from its JSON-shaped representation."""
        if not isinstance(payload, Mapping):
            raise TypeError("GitRepo payload must be an object")
        if payload.get("schema") != GIT_REPO_HANDLE_SCHEMA:
            raise ValueError("GitRepo payload schema is unsupported")
        binding = payload.get("binding")
        basis = payload.get("basis")
        if not isinstance(binding, str):
            raise TypeError("GitRepo payload binding must be a string")
        if not isinstance(basis, Mapping):
            raise TypeError("GitRepo payload basis must be an object")
        authority = payload.get("authority")
        if not isinstance(authority, list | tuple | set | frozenset):
            raise TypeError("GitRepo payload authority must be a list of strings")
        return cls(
            binding=binding,
            basis=GitRepoBasis.from_payload(basis),
            authority=_coerce_git_repo_authority(authority),
        )


def _coerce_git_repo_authority(authority: Iterable[str]) -> GitRepoAuthority:
    if isinstance(authority, str):
        raise TypeError("GitRepo authority must be an iterable of authority atoms, not a string")
    try:
        atoms = frozenset(authority)
    except TypeError as exc:
        raise TypeError("GitRepo authority must be an iterable of authority atoms") from exc
    invalid = sorted(repr(atom) for atom in atoms if not isinstance(atom, str) or atom not in _GIT_REPO_AUTHORITY_ATOMS)
    if invalid:
        raise ValueError(f"unsupported GitRepo authority atom(s): {', '.join(invalid)}")
    return cast("GitRepoAuthority", atoms)


def _validate_non_empty_str(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


__all__ = [
    "GIT_REPO_BASIS_SCHEMA",
    "GIT_REPO_HANDLE_SCHEMA",
    "GitRepo",
    "GitRepoAuthority",
    "GitRepoAuthorityAtom",
    "GitRepoBasis",
]
