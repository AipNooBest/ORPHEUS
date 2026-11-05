"""Mutable VM aliases are stored as identity, not flattened into equal values."""

import copy
import random
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import replay_values
from ow174.game.body_vm_snapshot import (
    BodyVMSnapshotError,
    export_component_vm,
    export_component_vm_with_identities,
    snapshot_digest,
)
from ow174.game.script import expr, graph, runtime


def fixture():
    model = graph.Graph({
        "index": 77, "header": {}, "fields": {}, "items": 2, "containers": {},
        "nodes": [{
            "pos": number, "class": "STU_TestPlainState", "uid": number + 10, "rid": number,
            "remote": -1, "state": number, "client_only": 0, "server_only": 0,
            "fields": {"m_2BBEEAB8": 1},
        } for number in range(2)],
        "unknown_nodes": [], "states": [0, 1], "entries": [], "remote": [], "sync_vars": [],
    })
    component = runtime.Component(456)
    first = component.create(model, init=False)
    second = component.create(model, init=False)
    first.state(0)
    first.state(1)
    second.state(0)
    return component, first, second, component.entity_var(11), component.entity_var(22)


def fingerprint(component):
    return snapshot_digest(export_component_vm(component))


def graph_record(document):
    objects = replay_values.decode(document["value"])["objects"]
    return next(row for row in objects if row["ref"] == ["graph", 77])


class BodyVMAliasTests(unittest.TestCase):
    def test_shared_empty_watchers_change_the_hash_and_cause_observable_cross_variable_notifications(self):
        separate, first, _, v1, v2 = fixture()
        shared, shared_first, _, shared_v1, shared_v2 = fixture()
        self.assertEqual(fingerprint(separate), fingerprint(shared))
        shared_v2.watchers = shared_v1.watchers
        self.assertNotEqual(fingerprint(separate), fingerprint(shared))

        ordinary_state = first.states[0]
        ordinary_state.active = True
        ordinary_state.watch(v1)
        shared_state = shared_first.states[0]
        shared_state.active = True
        shared_state.watch(shared_v1)
        self.assertNotIn(ordinary_state, v2.watchers)
        self.assertIn(shared_state, shared_v2.watchers)
        separate.set(22, 1)
        shared.set(22, 1)
        self.assertEqual(first.queue, [])
        self.assertEqual([(event.kind, event.state) for event in shared_first.queue],
                         [(runtime.CHANGED, shared_state)])

    def test_separate_empty_runtime_containers_are_distinct_from_shared_containers(self):
        for role in ("watchers", "links", "watched", "queue", "ticking", "deferred", "params", "natives"):
            with self.subTest(role=role):
                component, first, second, v1, v2 = fixture()
                if role in ("watchers", "links"):
                    left, right = v1, v2
                elif role in ("watched", "natives"):
                    left, right = first.states[0], first.states[1]
                else:
                    left, right = first, second
                self.assertIsNot(getattr(left, role), getattr(right, role))
                before = fingerprint(component)
                setattr(right, role, getattr(left, role))
                self.assertNotEqual(fingerprint(component), before)

    def test_empty_plain_list_dict_and_set_aliases_are_not_collapsed_by_value_equality(self):
        for container in (list, dict, set):
            with self.subTest(container=container.__name__):
                component, _, _, v1, v2 = fixture()
                v1.base, v2.base = container(), container()
                separate = fingerprint(component)
                v2.base = v1.base
                self.assertNotEqual(fingerprint(component), separate)

    def test_repeated_link_object_differs_from_two_equal_link_objects(self):
        component, first, _, variable, _ = fixture()
        first_link = runtime.Link(first.states[0], 1, 2.0, False, True)
        second_link = runtime.Link(first.states[0], 1, 2.0, False, True)
        self.assertEqual(first_link, second_link)
        self.assertIsNot(first_link, second_link)
        variable.links = [first_link, second_link]
        separate = fingerprint(component)
        variable.links[1] = first_link
        self.assertNotEqual(fingerprint(component), separate)

    def test_repeated_event_object_differs_from_two_equal_event_objects(self):
        component, first, _, _, _ = fixture()
        one = runtime.Event(20, 9, runtime.CHECK, state=first.states[0], transient=True)
        two = runtime.Event(20, 9, runtime.CHECK, state=first.states[0], transient=True)
        self.assertEqual(one, two)
        self.assertIsNot(one, two)
        first.queue = [one, two]
        separate = fingerprint(component)
        first.queue[1] = one
        self.assertNotEqual(fingerprint(component), separate)

    def test_normal_value_assignment_and_map_nesting_allow_repeated_mutable_values(self):
        component, first, _, v1, v2 = fixture()
        shared = [{"nested": []}]
        v1.base = shared
        first.write((expr.ENTITY, 22), shared)
        self.assertIs(v1.base, v2.base)
        v2.base = expr.Map(((7, shared), (7, shared)))
        aliased = fingerprint(component)
        v2.base = expr.Map(((7, copy.deepcopy(shared)), (7, copy.deepcopy(shared))))
        self.assertNotEqual(fingerprint(component), aliased)

    def test_equivalent_alias_topology_is_stable_across_independent_allocations(self):
        hashes = []
        for noise_size in (0, 1, 17, 100):
            allocation_noise = [{} for _ in range(noise_size)]
            component, first, second, v1, v2 = fixture()
            shared = {"nested": [b"same", -0.0]}
            v1.base = [shared, shared]
            v2.base = expr.Map(((5, shared),))
            first.params = [{None: shared}]
            second.params = first.params
            event = runtime.Event(3, 4, runtime.CHECK, state=first.states[0])
            first.queue = [event, event]
            hashes.append(fingerprint(component))
            self.assertEqual(len(allocation_noise), noise_size)
        self.assertEqual(len(set(hashes)), 1)

    def test_plain_list_dict_cycles_are_representable_stable_and_detached(self):
        documents = []
        for _ in range(2):
            component, _, _, variable, _ = fixture()
            sequence = []
            mapping = {"back": sequence}
            sequence.append(mapping)
            variable.base = sequence
            document = export_component_vm(component)
            detached = copy.deepcopy(document)
            self.assertEqual(snapshot_digest(document), snapshot_digest(detached))
            self.assertIs(sequence[0], mapping)
            self.assertIs(mapping["back"], sequence)
            documents.append(document)
        self.assertEqual(snapshot_digest(documents[0]), snapshot_digest(documents[1]))

    def test_actual_state_storage_dictionary_alias_differs_from_an_equal_shallow_copy(self):
        component, first, _, variable, _ = fixture()
        variable.base = vars(first.states[0]).copy()
        copied = fingerprint(component)
        variable.base = vars(first.states[0])
        self.assertNotEqual(fingerprint(component), copied)

    def test_graph_node_field_aliases_differ_from_equal_separate_definitions(self):
        component, first, _, _, _ = fixture()
        left, right = first.graph.nodes
        self.assertEqual(left.fields, right.fields)
        separate = export_component_vm(component)
        right.fields = left.fields
        shared = export_component_vm(component)
        self.assertNotEqual(graph_record(separate)["definition_sha256"],
                            graph_record(shared)["definition_sha256"])
        self.assertNotEqual(snapshot_digest(separate), snapshot_digest(shared))

    def test_vm_value_aliases_to_definition_storage_have_explicit_cross_domain_bindings(self):
        component, first, _, variable, _ = fixture()
        variable.base = dict(first.graph.nodes[0].fields)
        copied = export_component_vm(component)
        variable.base = first.graph.nodes[0].fields
        shared = export_component_vm(component)
        self.assertNotEqual(snapshot_digest(copied), snapshot_digest(shared))
        self.assertTrue(graph_record(shared)["storage_bindings"])

    def test_normal_cached_graph_and_node_reference_sharing_remains_supported(self):
        component, first, second, _, _ = fixture()
        self.assertIs(first.graph, second.graph)
        model = first.graph
        self.assertIs(model.nodes[0], model.states[0])
        self.assertIs(model.nodes[0], model._by_pos[0])
        self.assertIs(model.nodes[0], model._by_rid[0])
        document = export_component_vm(component)
        objects = replay_values.decode(document["value"])["objects"]
        self.assertEqual(sum(row["ref"] == ["graph", 77] for row in objects), 1)

    def test_sets_of_stable_vm_references_do_not_choose_registration_order(self):
        hashes = []
        for reverse in (False, True, False, True):
            component, first, _, variable, _ = fixture()
            # Only the set reaches this old instance, so visiting its members can otherwise
            # assign first-seen identities before instance.states establishes their order.
            hidden = component.create(first.graph, init=False)
            members = [hidden.state(0), hidden.state(1)]
            del component.instances[hidden.id]
            if reverse:
                members.reverse()
            variable.base = set(members)
            hashes.append(fingerprint(component))
        self.assertEqual(len(set(hashes)), 1)

    def test_unknown_set_members_cannot_introduce_an_address_based_identity(self):
        component, _, _, variable, _ = fixture()
        variable.base = {object()}
        with self.assertRaises(BodyVMSnapshotError):
            export_component_vm(component)

    def test_observation_preserves_source_container_and_record_identities(self):
        component, first, second, v1, v2 = fixture()
        shared = [b"stored", {"value": 3}]
        v1.base = shared
        v2.base = shared
        second.params = first.params
        event = runtime.Event(5, 6, runtime.CHECK, state=first.states[0])
        first.queue = [event, event]
        link = runtime.Link(first.states[0])
        v1.links = [link, link]
        identities = tuple(map(id, (shared, shared[1], first.params, second.params, first.queue,
                                    first.queue[0], first.queue[1], v1.links[0], v1.links[1],
                                    first.graph.nodes[0].fields, vars(first.states[0]))))
        sequence, rng = component.seq.next_value, component.rng.getstate()
        one = export_component_vm(component)
        two = export_component_vm(component)
        self.assertEqual(snapshot_digest(one), snapshot_digest(two))
        self.assertEqual(identities, tuple(map(id, (
            shared, shared[1], first.params, second.params, first.queue,
            first.queue[0], first.queue[1], v1.links[0], v1.links[1],
            first.graph.nodes[0].fields, vars(first.states[0]),
        ))))
        self.assertEqual(shared, [b"stored", {"value": 3}])
        self.assertEqual((event.time, event.seq, link.slot), (5, 6, 0))
        self.assertEqual((component.seq.next_value, component.rng.getstate()), (sequence, rng))

    def test_identity_sidecar_matches_the_ordinary_document_and_binds_one_shared_original_object(self):
        component, _, _, v1, v2 = fixture()
        shared = {"nested": []}
        v1.base = shared
        v2.base = shared
        document, identities = export_component_vm_with_identities(component)
        self.assertEqual(document, export_component_vm(component))
        self.assertIs(type(identities), tuple)
        self.assertEqual(len({sid for sid, _, _ in identities}), len(identities))
        self.assertEqual(len({id(obj) for _, obj, _ in identities}), len(identities))
        binding = [(sid, scope) for sid, obj, scope in identities if obj is shared]
        self.assertEqual(len(binding), 1)
        sid, scope = binding[0]
        self.assertEqual(scope, "vm")
        objects = replay_values.decode(document["value"])["objects"]
        bases = [dict(row["fields"])["base"] for row in objects if row["ref"] in (
            ["var", 0, 11], ["var", 0, 22],
        )]
        self.assertEqual([value["identity"] for value in bases], [["storage", sid]] * 2)

    def test_identity_sidecar_distinguishes_private_model_and_cross_domain_storage(self):
        component, first, _, v1, v2 = fixture()
        mixed = first.graph.nodes[0].fields
        private = []
        v1.base = mixed
        v2.base = private
        _, identities = export_component_vm_with_identities(component)

        def scope_of(original):
            rows = [scope for _, obj, scope in identities if obj is original]
            self.assertEqual(len(rows), 1)
            return rows[0]

        self.assertEqual(scope_of(private), "vm")
        self.assertEqual(scope_of(vars(first.states[0])), "vm")
        self.assertEqual(scope_of(first.graph.header), "model")
        self.assertEqual(scope_of(vars(first.graph)), "model")
        self.assertEqual(scope_of(mixed), "vm+model")

    def test_sidecar_excludes_argument_wrappers_and_all_observer_temporary_containers(self):
        component, first, _, _, _ = fixture()
        allowed, visited = set(), set()

        def collect(original):
            kind = type(original)
            if kind in (list, dict, set, runtime.Link, runtime.Event, random.Random,
                        runtime.EventSequence, runtime.ScriptBudgetError):
                allowed.add(id(original))
            if id(original) in visited:
                return
            visited.add(id(original))
            if kind is dict:
                for key, value in original.items():
                    collect(key)
                    collect(value)
            elif kind in (list, tuple, set, frozenset):
                for value in original:
                    collect(value)
            elif kind is runtime.Var:
                for name in runtime.Var.__slots__:
                    collect(getattr(original, name))
            elif kind in (runtime.Component, runtime.Instance, runtime.State, runtime.World,
                          graph.Graph, graph.Node, random.Random):
                collect(vars(original))

        collect(component)
        caller_roots = [first.states[0]]
        document, identities = export_component_vm_with_identities(component, extra_roots=caller_roots)
        self.assertEqual(document, export_component_vm(component, extra_roots=tuple(caller_roots)))
        self.assertFalse(any(obj is caller_roots for _, obj, _ in identities))
        self.assertTrue(all(id(obj) in allowed for _, obj, _ in identities))

    def test_graph_definition_signature_uses_local_topology_independent_of_vm_storage_numbering(self):
        component, _, _, variable, _ = fixture()
        before = export_component_vm(component)
        variable.base = {"unrelated": [[], {}, set()]}
        after = export_component_vm(component)
        self.assertNotEqual(snapshot_digest(before), snapshot_digest(after))
        self.assertEqual(graph_record(before)["definition_sha256"],
                         graph_record(after)["definition_sha256"])

    def test_derived_graph_sequences_are_checked_read_only_without_calling_cache_accessors(self):
        accessors = {name: mock.Mock(side_effect=AssertionError("called graph cache accessor"))
                     for name in ("owner_states", "remote_states", "presence_vars")}
        with mock.patch.multiple(graph.Graph, **accessors):
            component, _, _, _, _ = fixture()
            export_component_vm(component)
            for cache_name in ("_owner", "_remote", "_presence"):
                with self.subTest(cache=cache_name):
                    component, first, _, _, _ = fixture()
                    model = first.graph
                    if cache_name == "_owner":
                        model._owner.reverse()
                    elif cache_name == "_remote":
                        model.remote_nodes = list(model.nodes)
                        model._remote = list(reversed(model.nodes))
                    else:
                        model.sync_vars = [graph.SyncVar(0, 1, 0, 0), graph.SyncVar(1, 2, 0, 0)]
                        model._presence = list(reversed(model.sync_vars))
                    source = getattr(model, cache_name)
                    original = tuple(source)
                    with self.assertRaisesRegex(BodyVMSnapshotError, "derived cache"):
                        export_component_vm(component)
                    self.assertIs(getattr(model, cache_name), source)
                    self.assertEqual(tuple(source), original)


if __name__ == "__main__":
    unittest.main()
