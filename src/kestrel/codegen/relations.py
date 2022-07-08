"""Relationship mapping from Kestrel relation to STIX references.

STIX reference names may not be the original STIX reference name. The names
used here are pre-processed by :func:`firepit.raft.invert`. Check the function
for more details.

"""

import dateutil.parser
import datetime
from collections import defaultdict
import logging

from firepit.query import Column, Join, Query, Projection, Table, Unique

_logger = logging.getLogger(__name__)

stix_2_0_ref_mapping = {
    # (EntityX, Relate, EntityY): ([EntityX_STIX_Ref_i], [EntityY_STIX_Ref_i])
    # All STIX 2.0 refs enumerated
    # file
    ("file", "contained", "artifact"): (["content_ref"], []),
    ("directory", "contained", "directory"): (["contains_refs"], ["contains_refs"]),
    ("directory", "contained", "file"): (["contains_refs"], ["parent_directory_ref"]),
    ("archive-ext", "contained", "file"): (["contains_refs"], []),
    # email
    ("user-account", "owned", "email-addr"): ([], ["belongs_to_ref"]),
    ("email-addr", "created", "email-message"): ([], ["from_ref", "sender_ref"]),
    ("email-addr", "accepted", "email-message"): (
        [],
        ["to_refs", "cc_refs", "bcc_refs"],
    ),
    ("email-message", None, "artifact"): (["raw_email_ref", "body_raw_ref"], []),
    ("email-message", None, "file"): (
        ["body_raw_ref"],
        [],
    ),  # FIXME: should be mime-part-type?
    # ip address
    ("autonomous-system", "owned", "ipv4-addr"): ([], ["belongs_to_refs"]),
    ("autonomous-system", "owned", "ipv6-addr"): ([], ["belongs_to_refs"]),
    # network-traffic
    ("ipv4-addr", "created", "network-traffic"): ([], ["src_ref"]),
    ("ipv6-addr", "created", "network-traffic"): ([], ["src_ref"]),
    ("mac-addr", "created", "network-traffic"): ([], ["src_ref"]),
    ("domain-name", "created", "network-traffic"): ([], ["src_ref"]),
    ("artifact", "created", "network-traffic"): ([], ["src_payload_ref"]),
    ("mac-addr", None, "ipv4-addr"): ([], ["resolves_to_refs"]),
    ("mac-addr", None, "ipv6-addr"): ([], ["resolves_to_refs"]),
    ("http-request-ext", None, "artifact"): (["message_body_data_ref"], []),
    ("ipv4-addr", "accepted", "network-traffic"): ([], ["dst_ref"]),
    ("ipv6-addr", "accepted", "network-traffic"): ([], ["dst_ref"]),
    ("mac-addr", "accepted", "network-traffic"): ([], ["dst_ref"]),
    ("domain-name", "accepted", "network-traffic"): ([], ["dst_ref"]),
    ("artifact", "accepted", "network-traffic"): ([], ["dst_payload_ref"]),
    ("network-traffic", "contained", "network-traffic"): (
        ["encapsulated_by_ref"],
        ["encapsulated_by_ref"],
    ),
    # process
    ("process", "created", "network-traffic"): (["opened_connection_refs"], []),
    ("user-account", "owned", "process"): ([], ["creator_user_ref"]),
    ("process", "loaded", "file"): (["binary_ref"], []),
    # ("process", "created", "process"): (["child_refs"], ["parent_ref"]),
    ("process", "created", "process"): ([], ["parent_ref"]),
    # service
    ("windows-service-ext", "loaded", "file"): (["service_dll_refs"], []),
    ("windows-service-ext", "loaded", "user-account"): (["creator_user_ref"], []),
}

# FIXME: is this no longer needed?
# the first available attribute will be used to uniquely identify the entity
stix_2_0_identical_mapping = {
    # entity-type: id attributes candidates
    "directory": ("path",),
    "domain-name": ("value",),
    "email-addr": ("value",),
    "file": ("name",),  # optional in STIX standard
    "ipv4-addr": ("value",),
    "ipv6-addr": ("value",),
    "mac-addr": ("value",),
    "mutex": ("name",),
    # `pid` is optional in STIX standard
    # `first_observed` cannot be used since it may be wrong (derived from observation)
    # `command_line` or `name` may not be in data and cannot be used
    "process": ("pid", "name"),
    "software": ("name",),
    "url": ("value",),
    "user-account": ("user_id",),  # optional in STIX standard
    "windows-registry-key": ("key",),  # optional in STIX standard
}

stix_x_ibm_event_mapping = {
    # entity-type to ref in x-oca-event
    "process": "process_ref",
    "domain-name": "domain_ref",
    "file": "file_ref",
    "user-account": "user_ref",
    "windows-registry-key": "registry_ref",
    "network-traffic": "nt_ref",
    "x-oca-asset": "host_ref",
}

# no direction for generic relations
generic_relations = ["linked"]

all_relations = list(
    set([x[1] for x in stix_2_0_ref_mapping.keys() if x[1]] + generic_relations)
)


def get_entity_id_attribute(variable):
    # this function should always return something
    # if no entity id attribute found, fall back to record "id" by default
    # this works for:
    #   - no appriparite identifier attribute found given specific data
    #   - "network-traffic" (not in stix_2_0_identical_mapping)
    id_attr = "id"

    if variable.type in stix_2_0_identical_mapping:
        available_attributes = variable.store.columns(variable.entity_table)
        for attr in stix_2_0_identical_mapping[variable.type]:
            if attr in available_attributes:
                query = Query()
                query.append(Table(variable.entity_table))
                query.append(Projection([attr]))
                query.append(Unique())
                rows = variable.store.run_query(query).fetchall()
                all_values = [row[attr] for row in rows if row[attr]]
                if all_values:
                    id_attr = attr
                    break

    return id_attr


def are_entities_associated_with_x_ibm_event(entity_types):
    flags = [entity_type in stix_x_ibm_event_mapping for entity_type in entity_types]
    return all(flags)


def compile_generic_relation_to_pattern(return_type, input_type, input_var_name):
    comp_exps = []
    for relation, is_reversed in _enumerate_relations_between_entities(
        return_type, input_type
    ):
        comp_exps += _generate_paramstix_comparison_expressions(
            return_type, relation, input_type, is_reversed, input_var_name
        )
    pattern = "[" + " OR ".join(comp_exps) + "]"
    _logger.debug(f"generic relation pattern compiled: {pattern}")
    return pattern


def compile_specific_relation_to_pattern(
    return_type, relation, input_type, is_reversed, input_var_name
):
    comp_exps = _generate_paramstix_comparison_expressions(
        return_type, relation, input_type, is_reversed, input_var_name
    )
    pattern = "[" + " OR ".join(comp_exps) + "]"
    _logger.debug(f"specific relation pattern compiled: {pattern}")
    return pattern


def compile_identical_entity_search_pattern(var_name, var_struct, does_support_id):
    # "id" attribute may not be available for STIX 2.0 via STIX-shifter
    # so `does_support_id` is set to False in default kestrel config file
    attribute = get_entity_id_attribute(var_struct)
    if attribute == "id" and not does_support_id:
        pattern = None
    else:
        pattern = f"[{var_struct.type}:{attribute} = {var_name}.{attribute}]"
    _logger.debug(f"identical entity search pattern compiled: {pattern}")
    return pattern


def compile_x_ibm_event_search_flow_in_pattern(input_type, input_var_name):
    ref = stix_x_ibm_event_mapping[input_type]
    pattern = f"[x-oca-event:{ref}.id = {input_var_name}.id]"
    _logger.debug(f"x-oca-event flow in pattern compiled: {pattern}")
    return pattern


def compile_x_ibm_event_search_flow_out_pattern(return_type, input_event_var_name):
    ref = stix_x_ibm_event_mapping[return_type]
    pattern = f"[{return_type}:id = {input_event_var_name}.{ref}.id]"
    _logger.debug(f"x-oca-event flow out pattern compiled: {pattern}")
    return pattern


def _enumerate_relations_between_entities(return_type, input_type):
    # return: [(relation, is_reversed)]
    relations = []
    for (x, r, y) in stix_2_0_ref_mapping.keys():
        if x == return_type and y == input_type:
            relations.append((r, False))
        if y == return_type and x == input_type:
            relations.append((r, True))
    _logger.debug(
        f'enumerated relations between "{return_type}" and "{input_type}": {relations}'
    )
    return relations


def _generate_paramstix_comparison_expressions(
    return_type, relation, input_type, is_reversed, input_var_name
):
    (entity_x, entity_y) = (
        (input_type, return_type) if is_reversed else (return_type, input_type)
    )

    stix_src_refs, stix_tgt_refs = stix_2_0_ref_mapping[(entity_x, relation, entity_y)]

    comp_exps = []
    for stix_ref in stix_src_refs:
        if stix_ref.endswith("_refs"):
            comp_exps.append(f"{return_type}:id = {input_var_name}.{stix_ref}[*].id")
        else:
            comp_exps.append(f"{return_type}:id = {input_var_name}.{stix_ref}.id")

    for stix_ref in stix_tgt_refs:
        if stix_ref.endswith("_refs"):
            comp_exps.append(f"{return_type}:id = {input_var_name}.{stix_ref}[*].id")
        else:
            comp_exps.append(f"{return_type}:id = {input_var_name}.{stix_ref}.id")

    return comp_exps


def fine_grained_relational_process_filtering(
    local_var, prefetch_entity_table, store, config
):

    _logger.debug(
        f"start fine-grained relational process filtering for prefetched table: {prefetch_entity_table}"
    )

    query_ref = Query(
        [
            Table(local_var.entity_table),
            Join("__contains", "id", "=", "target_ref"),
            Join("observed-data", "source_ref", "=", "id"),
            # need to put the LEFT JOIN at last
            # so do not need to specify lhs for the first two JOINS
            Join(
                "process",
                "parent_ref",
                "=",
                "id",
                how="LEFT OUTER",
                lhs=local_var.entity_table,
            ),
            Projection(
                [
                    Column("pid", local_var.entity_table, "pid"),
                    Column("name", local_var.entity_table, "name"),
                    Column("pid", "process", "ppid"),
                    "first_observed",
                    "last_observed",
                ]
            ),
        ]
    )
    ref_rows = local_var.store.run_query(query_ref).fetchall()

    ref_processes = defaultdict(list)

    for row in ref_rows:
        if row["pid"]:
            process_name = row["name"]
            process_parent_pid = row["ppid"]
            process_start_time = dateutil.parser.isoparse(row["first_observed"])
            process_end_time = dateutil.parser.isoparse(row["last_observed"])
            ref_processes[row["pid"]].append(
                (process_name, process_parent_pid, process_start_time, process_end_time)
            )

    query_fil = Query(
        [
            Table(prefetch_entity_table),
            Join("__contains", "id", "=", "target_ref"),
            Join("observed-data", "source_ref", "=", "id"),
            # need to put the LEFT JOIN at last
            # so do not need to specify lhs for the first two JOINS
            Join(
                "process",
                "parent_ref",
                "=",
                "id",
                how="LEFT OUTER",
                lhs=prefetch_entity_table,
            ),
            Projection(
                [
                    Column("id", prefetch_entity_table, "id"),
                    Column("pid", prefetch_entity_table, "pid"),
                    Column("name", prefetch_entity_table, "name"),
                    Column("pid", "process", "ppid"),
                    "first_observed",
                    "last_observed",
                ]
            ),
        ]
    )
    fil_rows = store.run_query(query_fil).fetchall()

    fil_rows = [
        (
            r["pid"],
            r["name"],
            r["ppid"],
            dateutil.parser.isoparse(row["first_observed"]),
            dateutil.parser.isoparse(row["last_observed"]),
            r["id"],
        )
        for r in fil_rows
        if r["pid"]
    ]

    # Two-step search for matched processes
    # 1. pivot process search
    # 2. precise process search

    # search for pivot_rows in fil_rows those has more info than ref_rows, e.g., process
    # name or ppid.
    #
    # in real implementation, for performance, ref_rows and pivot_rows are implemented as
    # ref_processes and pivot_processes.
    #
    # two situations worth mentioning:
    # - in Linux, a new process will be forked, then exec to change name. In this case,
    #   we need to search for pivot_rows to identify process with even name changed,
    #   then get all records of both process before name change and after name change.
    # - ppid is useful to identify a process with pid, however, in one situation, the
    #   ppid data is not available in the first phase of FIND (creating of local_var
    #   using deref in firepit)---FIND parent process of current process. This is because
    #   most datasource does not store *parent parent process pid* for deref to get ppid
    #   of the parent. In this case, we need to search for pivot_rows to infer the ppid.
    pivot_processes = defaultdict(list)
    for fil_row in fil_rows:
        pid = fil_row[0]
        fil_row = fil_row[1:-1]
        for ref_row in ref_processes[pid]:
            if _identical_process_check(fil_row, ref_row, config):
                pivot_processes[pid].append(fil_row)
                break

    _logger.debug(
        f"found {sum(map(len, pivot_processes.values()))} pivot rows out of {len(fil_rows)} raw prefetched results."
    )

    # search for precise process match based on pivot results
    filtered_ids = []
    for fil_row in fil_rows:
        pid = fil_row[0]
        rid = fil_row[-1]
        fil_row = fil_row[1:-1]
        for pivot_row in pivot_processes[pid]:
            if _identical_process_check(fil_row, pivot_row, config):
                filtered_ids.append(rid)
                break

    filtered_ids = list(set(filtered_ids))

    _logger.debug(
        f"found {len(filtered_ids)} out of {len(fil_rows)} raw prefetched results to be true relational process records."
    )

    return filtered_ids


def _identical_process_check(fil_row, ref_row, config):
    pnc_start_offset = datetime.timedelta(
        seconds=config["process_name_change_timerange_start_offset"]
    )
    pnc_stop_offset = datetime.timedelta(
        seconds=config["process_name_change_timerange_stop_offset"]
    )
    pls_start_offset = datetime.timedelta(
        seconds=config["process_lifespan_start_offset"]
    )
    pls_stop_offset = datetime.timedelta(seconds=config["process_lifespan_stop_offset"])

    fil_pname, fil_ppid, fil_start_time, fil_end_time = fil_row
    ref_pname, ref_ppid, ref_start_time, ref_end_time = ref_row
    if (
        (
            fil_pname
            and fil_pname == ref_pname
            and fil_start_time > ref_start_time + pls_start_offset
            and fil_start_time < ref_end_time + pls_stop_offset
        )
        or (
            fil_ppid
            and fil_ppid == ref_ppid
            and fil_start_time > ref_start_time + pls_start_offset
            and fil_start_time < ref_end_time + pls_stop_offset
        )
        or (
            # name changed process, Linux fork+exec handled
            fil_start_time > ref_start_time + pnc_start_offset
            and fil_start_time < ref_end_time + pnc_stop_offset
        )
        or (
            # name changed process, Linux fork+exec handled
            fil_end_time > ref_start_time + pnc_start_offset
            and fil_end_time < ref_end_time + pnc_stop_offset
        )
    ):
        return True
    else:
        return False
