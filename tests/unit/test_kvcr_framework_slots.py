# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""Framework-managed local DRAM slots: the framework places, KVCR records."""

import ctypes
from unittest.mock import Mock

import pytest
from _kvcr_test_utils import (
    FakeBytesControl,
    FakeNixlAgent,
    FakePrimaryPinning,
    _buffer_region,
    _mem_descriptor,
    _new_kvcr,
    _new_local_kvcr,
    _op_entries,
    _poll_until,
    _RecordingFIFOPolicy,
    _recovered_record,
    _start_write_message,
    _wait_until,
)

from kvcr import KVCR, KVCRBindings
from kvcr.config import KVCRBackendConfigs, KVCRConfig, LocalDramOptions
from kvcr.recovery_journal import (
    _attach_journal,
    _recovery_frames,
    _RecoveryMirror,
    install_recovery_records,
)
from kvcr.types import (
    BlockKey,
    CacheTier,
    InventoryEvent,
    QueryStatus,
    RegionDescriptor,
)

_BLOCK = 16
_SLOTS = 4


def _framework_kvcr(local, events=None, framework=None, *, agent=None, policy=None):
    return _new_local_kvcr(
        agent or FakeNixlAgent(),
        local,
        _SLOTS,
        inventory_sink=events.append if events is not None else None,
        policy=policy,
        framework_regions=(
            [_buffer_region(framework, _BLOCK)] if framework is not None else None
        ),
        framework_managed_slots=True,
    )


def _journaled(kvcr: KVCR) -> Mock:
    journal = Mock()
    journal.publish.return_value = True
    _attach_journal(kvcr._core._local_dram, journal)
    return journal


def _mirror(journal: Mock) -> dict[BlockKey, list[tuple[str, int]]]:
    mirror = _RecoveryMirror(("",))
    for call in journal.publish.call_args_list:
        mirror.apply(*call.args)
    return {
        key: record.local_dram.slots for key, record in mirror.take_records().items()
    }


def test_committed_slots_are_readable_journaled_and_announced() -> None:
    local = ctypes.create_string_buffer(_BLOCK * _SLOTS)
    events: list[InventoryEvent] = []
    kvcr = _framework_kvcr(local, events)
    journal = _journaled(kvcr)
    first, second = BlockKey(b"k0"), BlockKey(b"k1")

    assert kvcr.local_dram_pools() == [("", ctypes.addressof(local), len(local))]
    kvcr.commit_framework_slots({first: [("", 2)], second: [("", 0)]})

    assert kvcr.query((first, second)) == [(QueryStatus.HIT, CacheTier.LOCAL_G2)] * 2
    assert kvcr.local_dram_residency() == {first: [("", 2)], second: [("", 0)]}
    assert _mirror(journal) == {first: [("", 2)], second: [("", 0)]}
    assert events == [InventoryEvent((first, second), CacheTier.LOCAL_G2, False)]
    state = kvcr._core._local_dram.telemetry_state()
    assert state["local_g2_free_slots"] == _SLOTS - 2
    assert state["local_g2_evictable_slots"] == 0


def test_kvcr_never_places_or_evicts_a_framework_slot() -> None:
    local = ctypes.create_string_buffer(_BLOCK * _SLOTS)
    framework = ctypes.create_string_buffer(_BLOCK)
    kvcr = _framework_kvcr(local, framework=framework)
    resident, absent = BlockKey(b"resident"), BlockKey(b"absent")
    kvcr.commit_framework_slots({resident: [("", 1)]})

    deposit = kvcr.deposit({absent: [_mem_descriptor()]})
    fetch = kvcr.fetch((absent,))

    assert dict(kvcr.poll_completed()) == {
        deposit: _op_entries({absent: False}),
        fetch: _op_entries({absent: False}),
    }
    assert kvcr.local_dram_residency() == {resident: [("", 1)]}


def test_retire_waits_for_kvcr_readers_and_journals_before_reuse() -> None:
    local = ctypes.create_string_buffer(_BLOCK * _SLOTS)
    events: list[InventoryEvent] = []
    kvcr = _framework_kvcr(local, events)
    journal = _journaled(kvcr)
    key, replacement = BlockKey(b"k0"), BlockKey(b"k1")
    kvcr.commit_framework_slots({key: [("", 3)]})
    fetch = kvcr.fetch((key,))
    claim = dict(kvcr.poll_completed())[fetch][key].release_handle
    published = journal.publish.call_count

    # A reader holds the slot: the framework must pick another victim.
    assert kvcr.try_retire_framework_slots([key]) == {key: False}
    assert journal.publish.call_count == published
    with pytest.raises(ValueError, match="already holds"):
        kvcr.commit_framework_slots({replacement: [("", 3)]})

    assert kvcr.release((claim,)) == [(claim, True)]
    assert kvcr.try_retire_framework_slots([key, key]) == {key: True}
    assert _mirror(journal) == {}
    assert kvcr.query((key,)) == [(QueryStatus.MISS, None)]
    assert events[-1] == InventoryEvent((key,), CacheTier.LOCAL_G2, True)
    assert key not in kvcr._core._block_record_map

    kvcr.commit_framework_slots({replacement: [("", 3)]})
    assert _mirror(journal) == {replacement: [("", 3)]}


@pytest.mark.parametrize(
    "blocks, message",
    [
        ({BlockKey(b"new"): [("", _SLOTS)]}, "out of range"),
        ({BlockKey(b"new"): [("", -1)]}, "out of range"),
        ({BlockKey(b"new"): [("", 0)]}, "already holds a block"),
        ({BlockKey(b"held"): [("", 2)]}, "already holds"),
        ({BlockKey(b"new"): []}, "no local DRAM slots"),
        ({BlockKey(b"new"): [("missing", 2)]}, "expected layout"),
        ({BlockKey(b"a"): [("", 2)], BlockKey(b"b"): [("", 2)]}, "already holds"),
    ],
)
def test_a_refused_commit_records_nothing(blocks, message) -> None:
    local = ctypes.create_string_buffer(_BLOCK * _SLOTS)
    kvcr = _framework_kvcr(local)
    kvcr.commit_framework_slots({BlockKey(b"held"): [("", 0)]})
    journal = _journaled(kvcr)

    with pytest.raises(ValueError, match=message):
        kvcr.commit_framework_slots({BlockKey(b"ok"): [("", 1)], **blocks})

    assert kvcr.local_dram_residency() == {BlockKey(b"held"): [("", 0)]}
    journal.publish.assert_not_called()


def test_retiring_an_unknown_block_changes_nothing() -> None:
    local = ctypes.create_string_buffer(_BLOCK * _SLOTS)
    kvcr = _framework_kvcr(local)
    held = BlockKey(b"held")
    kvcr.commit_framework_slots({held: [("", 0)]})

    with pytest.raises(ValueError, match="does not hold"):
        kvcr.try_retire_framework_slots([held, BlockKey(b"unknown")])

    assert kvcr.local_dram_residency() == {held: [("", 0)]}


def test_recovered_blocks_are_handed_to_the_framework_and_keep_their_slots() -> None:
    local = ctypes.create_string_buffer(_BLOCK * _SLOTS)
    kvcr = _framework_kvcr(local)
    recovered = BlockKey(b"recovered")
    install_recovery_records(kvcr._core, {recovered: _recovered_record(g2=[("", 1)])})

    assert kvcr.local_dram_residency() == {recovered: [("", 1)]}
    assert kvcr._core._local_dram.telemetry_state()["local_g2_free_slots"] == 3
    with pytest.raises(ValueError, match="already holds a block"):
        kvcr.commit_framework_slots({BlockKey(b"new"): [("", 1)]})
    assert kvcr.try_retire_framework_slots([recovered]) == {recovered: True}
    kvcr.commit_framework_slots({BlockKey(b"new"): [("", 1)]})


def test_framework_slot_calls_need_framework_managed_slots() -> None:
    local = ctypes.create_string_buffer(_BLOCK * _SLOTS)
    kvcr = _new_local_kvcr(FakeNixlAgent(), local, _SLOTS)

    with pytest.raises(RuntimeError, match="not framework-managed"):
        kvcr.commit_framework_slots({BlockKey(b"k"): [("", 0)]})
    with pytest.raises(RuntimeError, match="not framework-managed"):
        kvcr.try_retire_framework_slots([BlockKey(b"k")])


def test_framework_managed_slots_requires_local_dram() -> None:
    with pytest.raises(ValueError, match="requires local DRAM"):
        KVCR(
            KVCRConfig(
                nixl_agent_name="target",
                pool_layouts=[("", _BLOCK)],
                nixl_listen_port=1,
                framework_managed_slots=True,
            ),
            KVCRBindings(lambda keys: 0, lambda: (), lambda handle: False),
            KVCRBackendConfigs(),
        )


def test_a_peer_read_from_a_framework_slot_blocks_its_retire_until_done() -> None:
    local = ctypes.create_string_buffer(b"p" * _BLOCK * _SLOTS, _BLOCK * _SLOTS)
    agent, control = FakeNixlAgent(), FakeBytesControl()
    source = _new_kvcr(
        agent,
        FakePrimaryPinning(),
        control,
        KVCRConfig(
            nixl_agent_name="source",
            pool_layouts=[("", _BLOCK)],
            framework_managed_slots=True,
        ),
        name="source",
        local_dram=LocalDramOptions([("", ctypes.addressof(local), len(local))]),
        framework_regions=[RegionDescriptor(addr=4096, size=_BLOCK, count=4)],
    )
    key = BlockKey(b"shared")
    try:
        source.commit_framework_slots({key: [("", 2)]})
        agent.state = "PROC"
        control.incoming.append(_start_write_message(12, key, target_agent="target"))
        _poll_until(source, lambda _: len(agent.xfers) == 1)

        # Served straight from the framework's slot, with no framework pin.
        assert agent.xfers[0][1] == [(ctypes.addressof(local) + 2 * _BLOCK, _BLOCK, 0)]
        assert source.try_retire_framework_slots([key]) == {key: False}

        agent.state = "DONE"
        residency = source._core._block_record_map[key].local_dram
        _poll_until(source, lambda _: residency.claim_count == 0)
        assert source.try_retire_framework_slots([key]) == {key: True}
    finally:
        agent.state = "DONE"
        source.close()


def test_a_delivery_from_a_framework_slot_blocks_its_retire_until_done() -> None:
    local = ctypes.create_string_buffer(b"d" * _BLOCK * _SLOTS, _BLOCK * _SLOTS)
    framework = ctypes.create_string_buffer(_BLOCK)
    agent = FakeNixlAgent()
    kvcr = _framework_kvcr(local, framework=framework, agent=agent)
    key = BlockKey(b"k0")
    kvcr.commit_framework_slots({key: [("", 1)]})

    agent.state = "PROC"
    deliver = kvcr.deliver({key: [_mem_descriptor()]})
    _wait_until(lambda: bool(agent.transfers))
    assert kvcr.try_retire_framework_slots([key]) == {key: False}

    agent.state = "DONE"
    assert _poll_until(kvcr, bool) == [(deliver, _op_entries({key: True}))]
    assert framework.raw == b"d" * _BLOCK
    assert kvcr.try_retire_framework_slots([key]) == {key: True}


def test_framework_slots_drive_the_policy_but_never_its_eviction() -> None:
    local = ctypes.create_string_buffer(_BLOCK * _SLOTS)
    policy = _RecordingFIFOPolicy()
    kvcr = _framework_kvcr(local, policy=policy)
    keys = [BlockKey(bytes([index])) for index in range(_SLOTS)]

    kvcr.commit_framework_slots({key: [("", index)] for index, key in enumerate(keys)})
    assert [(meta.block_key, source) for meta, source in policy.ingested] == [
        (key, CacheTier.FW_G2) for key in keys
    ]
    # A full pool never makes KVCR pick a victim of its own.
    assert policy.scored == []
    assert policy.decided == []

    assert kvcr.try_retire_framework_slots(keys[:1]) == {keys[0]: True}
    assert [meta.block_key for meta, _ in policy.decided] == keys[:1]
    assert [meta.block_key for meta in policy.removed] == keys[:1]


def test_a_framework_pool_survives_a_guard_serving_it_and_comes_back_whole() -> None:
    """Primary journal -> Guard mirror and service -> handback -> new primary."""
    primary_pool = ctypes.create_string_buffer(_BLOCK * _SLOTS)
    primary = _framework_kvcr(primary_pool)
    journal = _journaled(primary)
    kept, retired = BlockKey(b"kept"), BlockKey(b"retired")
    primary.commit_framework_slots({kept: [("", 1)], retired: [("", 3)]})
    assert primary.try_retire_framework_slots([retired]) == {retired: True}
    mirror = _RecoveryMirror(("",))
    for call in journal.publish.call_args_list:
        mirror.apply(*call.args)

    # The Guard places slots itself, and must not reuse the one still named.
    guard_pool = ctypes.create_string_buffer(_BLOCK * _SLOTS)
    framework = ctypes.create_string_buffer(_BLOCK)
    guard_agent = FakeNixlAgent()
    guard_agent.state = "DONE"
    guard = _new_local_kvcr(
        guard_agent,
        guard_pool,
        _SLOTS,
        framework_regions=[_buffer_region(framework, _BLOCK)],
    )
    install_recovery_records(guard._core, mirror.take_records())
    assert guard.query((kept, retired)) == [
        (QueryStatus.HIT, CacheTier.LOCAL_G2),
        (QueryStatus.MISS, None),
    ]
    added = BlockKey(b"added-by-guard")
    deposit = guard.deposit({added: [_mem_descriptor()]})
    assert _poll_until(guard, bool) == [(deposit, _op_entries({added: True}))]
    (added_slot,) = guard.local_dram_residency()[added]
    assert added_slot != ("", 1)

    handback = _RecoveryMirror(("",))
    for frame in _recovery_frames(guard._core._block_record_map):
        handback.apply(*frame)
    replacement = _framework_kvcr(ctypes.create_string_buffer(_BLOCK * _SLOTS))
    install_recovery_records(replacement._core, handback.take_records())

    assert replacement.local_dram_residency() == {
        kept: [("", 1)],
        added: [added_slot],
    }
    with pytest.raises(ValueError, match="already holds a block"):
        replacement.commit_framework_slots({BlockKey(b"new"): [added_slot]})


def test_a_retire_is_journaled_while_its_slot_is_still_taken() -> None:
    local = ctypes.create_string_buffer(_BLOCK * _SLOTS)
    kvcr = _framework_kvcr(local)
    key = BlockKey(b"k0")
    kvcr.commit_framework_slots({key: [("", 2)]})
    taken_at_publish = []
    journal = Mock()

    def publish(record_type, block_key, payload):
        taken_at_publish.append(2 in kvcr._core._local_dram._framework_slots[""])
        return True

    journal.publish.side_effect = publish
    _attach_journal(kvcr._core._local_dram, journal)

    assert kvcr.try_retire_framework_slots([key]) == {key: True}
    # No caller can be told the slot is free before the removal is recoverable.
    assert taken_at_publish == [True]
    assert 2 not in kvcr._core._local_dram._framework_slots[""]


def test_framework_refs_address_the_pool_itself_and_register_it_once() -> None:
    local = ctypes.create_string_buffer(_BLOCK * _SLOTS)
    local[_BLOCK : 2 * _BLOCK] = b"s" * _BLOCK
    agent = FakeNixlAgent()
    agent.state = "DONE"
    kvcr = _framework_kvcr(local, agent=agent)
    key = BlockKey(b"k0")
    kvcr.commit_framework_slots({key: [("", 1)]})

    # A framework element i is pool slot i; no separate framework buffer exists.
    deliver = kvcr.deliver({key: [_mem_descriptor(3)]})
    assert _poll_until(kvcr, bool) == [(deliver, _op_entries({key: True}))]
    assert local.raw[3 * _BLOCK :] == b"s" * _BLOCK
    assert agent.registrations == [
        ([(ctypes.addressof(local), len(local), 0, "")], "DRAM")
    ]
