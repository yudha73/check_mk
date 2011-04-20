#!/usr/bin/python
# encoding: utf-8

import config, re, pprint, time
from lib import *


# Python 2.3 does not have 'set' in normal namespace.
# But it can be imported from 'sets'
try:
    set()
except NameError:
    from sets import Set as set



config.declare_permission_section("bi", "BI - Check_MK Business Intelligence")
config.declare_permission("bi.see_all",
        "See all hosts and services",
        "With this permission set, the BI aggregation rules are applied to all "
        "hosts and services - not only those the user is a contact for. If you "
        "remove this permissions then the user will see incomplete aggregation "
        "trees with status based only on those items.",
        [ "admin", "guest" ])

#      ____                _              _       
#     / ___|___  _ __  ___| |_ __ _ _ __ | |_ ___ 
#    | |   / _ \| '_ \/ __| __/ _` | '_ \| __/ __|
#    | |__| (_) | | | \__ \ || (_| | | | | |_\__ \
#     \____\___/|_| |_|___/\__\__,_|_| |_|\__|___/
#                                                 

# type of rule parameters
SINGLE = 'single'
MULTIPLE = 'multi'

# possible aggregated states
MISSING = -2
PENDING = -1
OK = 0
WARN = 1
CRIT = 2
UNKNOWN = 3
UNAVAIL = 4

# character that separates sites and hosts
SITE_SEP = '#'


#      ____                      _ _       _   _             
#     / ___|___  _ __ ___  _ __ (_) | __ _| |_(_) ___  _ __  
#    | |   / _ \| '_ ` _ \| '_ \| | |/ _` | __| |/ _ \| '_ \ 
#    | |__| (_) | | | | | | |_) | | | (_| | |_| | (_) | | | |
#     \____\___/|_| |_| |_| .__/|_|_|\__,_|\__|_|\___/|_| |_|
#                         |_|                                

# global variables
g_cache = {}                # per-user cache
g_config_information = None # for invalidating cache after config change

# Load the static configuration of all services and hosts (including tags)
# without state.
def load_services():
    services = {}
    html.live.set_prepend_site(True)
    html.live.set_auth_domain('bi')
    data = html.live.query("GET hosts\nColumns: name custom_variable_names custom_variable_values services\n")
    html.live.set_prepend_site(False)
    html.live.set_auth_domain('read')

    for site, host, varnames, values, svcs in data:
        vars = dict(zip(varnames, values))
        tags = vars.get("TAGS", "").split(" ")
        services[(site, host)] = (tags, svcs)

    return services
        
# Keep complete list of time stamps of configuration
# and start of each site. Unreachable sites are registered
# with 0.
def cache_needs_update():
    new_config_information = [tuple(config.modification_timestamps)]
    for site in html.site_status.values():
        new_config_information.append(site.get("program_start", 0))

    if new_config_information != g_config_information:
        return new_config_information
    else:
        return False

def reset_cache_status():
    global did_compilation
    did_compilation = False
    global used_cache 
    used_cache = False

def reused_compilation():
    return used_cache and not did_compilation

# Precompile the forest of BI rules. Forest? A collection of trees.
# The compiled forest does not contain any regular expressions anymore.
# Everything is resolved. Sites, hosts and services are hardcoded. The
# aggregation functions are still left as names. That way the forest
# printable (and storable in Python syntax to a file).
def compile_forest(user):
    global g_cache
    global g_user_cache

    new_config_information = cache_needs_update()
    if new_config_information: # config changed are Nagios restarted, clear cache
        global g_cache
        g_cache = {}
        global g_config_information
        g_config_information = new_config_information

    # Try to get data from per-user cache:
    cache = g_cache.get(user)
    if cache:
        # make sure, BI permissions have not changed since last time
        if cache["see_all"] == config.may("bi.see_all"):
            g_user_cache = cache
            global used_cache
            used_cache = True
            return cache["forest"]

    global did_compilation
    did_compilation = True

    cache = {
        "forest" :            {},
        "host_aggregations" : {},
        "affected_hosts" :    {},
        "affected_services":  {},
        "services" : load_services(),
        "see_all" : config.may("bi.see_all"),
    }
    g_user_cache = cache

    for entry in config.aggregations:
        if len(entry) < 3:
            raise MKConfigError("<h1>Invalid aggregation <tt>%s</tt></h1>"
             "must have at least 3 entries (has %d)" % (entry, len(entry)))
        
        group = entry[0]
        new_entries = compile_rule_node(entry[1:], 0)

        for entry in new_entries:
            remove_empty_nodes(entry)

        new_entries = [ e for e in new_entries if len(e[3]) > 0 ]
        
        # enter new aggregations into dictionary for that group
        entries = cache["forest"].get(group, [])
        entries += new_entries
        cache["forest"][group] = entries

        # Update several global speed-up indices
        for aggr in new_entries:
            req_hosts = aggr[0]

            # All single-host aggregations looked up per host
            if len(req_hosts) == 1:
                host = req_hosts[0] # pair of (site, host)
                entries = cache["host_aggregations"].get(host, [])
                entries.append((group, aggr))
                cache["host_aggregations"][host] = entries

            # All aggregations containing a specific host
            for h in req_hosts:
                entries = cache["affected_hosts"].get(h, [])
                entries.append((group, aggr))
                cache["affected_hosts"][h] = entries

            # All aggregations containing a specific service
            services = find_all_leaves(aggr)
            for s in services: # triples of site, host, service
                entries = cache["affected_services"].get(s, []) 
                entries.append((group, aggr))
                cache["affected_services"][s] = entries

    # Remember successful compile in cache
    g_cache[user] = cache


# Execute an aggregation rule, but prepare arguments 
# and iterate FOREACH first
def compile_rule_node(calllist, lvl):
    # Lookup rule source code        
    rulename, arglist = calllist[-2:]
    if rulename not in config.aggregation_rules:
        raise MKConfigError("<h1>Invalid configuration in variable <tt>aggregations</tt></h1>"
                "There is no rule named <tt>%s</tt>. Available are: <tt>%s</tt>" % 
                (rulename, "</tt>, </tt>".join(config.aggregation_rules.keys())))
    rule = config.aggregation_rules[rulename]

    # Execute FOREACH: iterate over matching hosts/services and 
    # for each hit create an argument list where $1$, $2$, ... are
    # substituted with matched strings.
    if calllist[0] in [ config.FOREACH_HOST, config.FOREACH_SERVICE ]:
        matches = find_matching_services(calllist[0], calllist[1:])
        new_elements = []
        for match in matches:
            args = [ substitute_matches(a, match) for a in arglist ]
            new_elements += compile_aggregation_rule(rule, args, lvl)
        return new_elements

    else:
        return compile_aggregation_rule(rule, arglist, lvl)


def find_matching_services(what, calllist):
    # honor list of host tags preceding the host_re
    if type(calllist[0]) == list:
        required_tags = calllist[0]
        calllist = calllist[1:]
    else:
        required_tags = []

    host_re = calllist[0]
    if what == config.FOREACH_HOST:
        service_re = config.HOST_STATE
    else:
        service_re = calllist[1]

    honor_site = SITE_SEP in host_re

    matches = set([])

    for (site, hostname), (tags, services) in g_user_cache["services"].items():
        host_matches = None
        if not match_host_tags(tags, required_tags):
            continue

        # For regex to have '$' anchor for end. Users might be surprised
        # to get a prefix match on host names. This is almost never what
        # they want. For services this is useful, however.
        if host_re.endswith("$"):
            anchored = host_re
        else:
            anchored = host_re + "$"

        # In order to distinguish hosts with the same name on different
        # sites we prepend the site to the host name. If the host specification
        # does not contain the site separator - though - we ignore the site
        # an match the rule for all sites.
        if honor_site:
            host_matches = do_match(anchored, "%s%s%s" % (site, SITE_SEP, hostname))
        else:
            host_matches = do_match(anchored, hostname)

        if host_matches != None:
            if config.HOST_STATE in service_re:
                matches.add(host_matches)
            else:
                for service in services:
                    svc_matches = do_match(service_re, service)
                    if svc_matches != None:
                        matches.add(host_matches + svc_matches)

    matches = list(matches)
    matches.sort()
    return matches

def do_match(reg, text):
    mo = regex(reg).match(text)
    if not mo:
        return None
    else:
        return tuple(mo.groups())


def substitute_matches(arg, match):
    for n, m in enumerate(match):
        arg = arg.replace("$%d$" % (n+1), m)
    return arg


# Debugging function
def render_forest():
    for group, trees in g_user_cache["forest"].items():
        html.write("<h2>%s</h2>" % group)
        for tree in trees:
            ascii = render_tree(tree)
            html.write("<pre>\n" + ascii + "<pre>\n")

# Debugging function
def render_tree(node, indent = ""):
    h = ""
    if len(node) == 3: # leaf node
        h += indent + "S/H/S: %s/%s/%s\n" % (node[1][0], node[1][1], node[2])
    else:
        h += indent + "Aggregation:\n"
        indent += "    "
        h += indent + "Description:  %s\n" % node[1]
        h += indent + "Needed Hosts: %s\n" % " ".join([("%s/%s" % h_s) for h_s in node[0]])
        h += indent + "Aggregation:  %s\n" % node[2]
        h += indent + "Nodes:\n"
        for node in node[3]:
            h += render_tree(node, indent + "  ")
        h += "\n"
    return h


# Compute dictionary of arguments from arglist and
# actual values
def make_arginfo(arglist, args):
    arginfo = {}
    for name, value in zip(arglist, args):
        if name[0] == 'a':
            expansion = SINGLE
            name = name[1:]
        elif name[-1] == 's':
            expansion = MULTIPLE
            name = name[:-1]
        else:
            raise MKConfigError("Invalid argument name %s. Must begin with 'a' or end with 's'." % name)
        arginfo[name] = (expansion, value)
    return arginfo

def find_all_leaves(node):
    # leaf node: ( NEEDED_HOSTS, HOSTSPEC, SERVICE )
    if len(node) == 3:
        needed_hosts, (site, host), service = node 
        return [ (site, host, service) ]

    # rule node: ( NEEDED_HOSTS, DESCRIPTION, FUNCNAME, NODES )
    else:
        entries = []
        for n in node[3]:
            entries += find_all_leaves(n)
        return entries

def remove_empty_nodes(node):
    if len(node) == 3: # leaf node
        return node
    else:
        subnodes = node[3]
        for i in range(0, len(subnodes)):
            remove_empty_nodes(subnodes[i])
        for i in range(0, len(subnodes))[::-1]:
            if node_is_empty(subnodes[i]):
                del subnodes[i]

def node_is_empty(node):
    if len(node) == 3:
        return False # leaf node
    else:
        return len(node[3]) == 0 

    
# Precompile one aggregation rule. This outputs a list of trees.
# The length of this list is current either 0 or 1
def compile_aggregation_rule(rule, args, lvl = 0):
    # When compiling root nodes we essentially create 
    # complete top-level aggregations. In that case we
    # need to deal with REMAINING-entries
    if lvl == 0:
        global g_remaining_refs
        g_remaining_refs = []

    if len(rule) != 4:
        raise MKConfigError("<h3>Invalid aggregation rule</h1>"
                "Aggregation rules must contain four elements: description, argument list, "
                "aggregation function and list of nodes. Your rule has %d elements: "
                "<pre>%s</pre>" % (len(rule), pprint.pformat(rule)))

    if lvl == 50:
        raise MKConfigError("<h3>Depth limit reached</h3>"
                "The nesting level of aggregations is limited to 50. You either configured "
                "too many levels or built an infinite recursion. This happened in rule <pre>%s</pre>"
                  % pprint.pformat(rule))

    description, arglist, funcname, nodes = rule

    # check arguments and convert into dictionary
    if len(arglist) != len(args):
        raise MKConfigError("<h1>Invalid rule usage</h1>"
                "The rule '%s' needs %d arguments: <tt>%s</tt><br>"
                "You've specified %d arguments: <tt>%s</tt>" % (
                    description, len(arglist), repr(arglist), len(args), repr(args)))

    arginfo = dict(zip(arglist, args))
    inst_description = subst_vars(description, arginfo)

    elements = []

    for node in nodes:
        # Each node can return more than one incarnation (due to regexes in 
        # leaf nodes and FOREACH in rule nodes)

        if node[1] in [ config.HOST_STATE, config.REMAINING ]:
            new_elements = compile_leaf_node(subst_vars(node[0], arginfo), node[1])
            new_new_elements = []
            for entry in new_elements:
                # Postpone: remember reference to list where we need to add
                # remaining services of host
                if entry[0] == config.REMAINING:
                    # create unique pointer which we find later
                    placeholder = ([], (None, None), str(len(g_remaining_refs)))
                    g_remaining_refs.append((entry[1], elements, placeholder))
                    new_new_elements.append(placeholder)
                else:
                    new_new_elements.append(entry)
            new_elements = new_new_elements

        elif type(node[-1]) != list:
            new_elements = compile_leaf_node(subst_vars(node[0], arginfo), subst_vars(node[1], arginfo))
        else:
            # substitute our arguments in rule arguments
            rule_args = [ subst_vars(a, arginfo) for a in node[-1] ]
            rule_parts = tuple([ subst_vars(part, arginfo) for part in node[:-1] ])
            new_elements = compile_rule_node(rule_parts + (rule_args,), lvl + 1)

        elements += new_elements

    needed_hosts = set([])
    for element in elements:
        needed_hosts.update(element[0])

    aggregation = (list(needed_hosts), inst_description, funcname, elements)

    # Handle REMAINING references, if we are a root node
    if lvl == 0:
        for hostspec, ref, placeholder in g_remaining_refs:
            new_entries = find_remaining_services(hostspec, aggregation)
            where_to_put = ref.index(placeholder)
            ref[where_to_put:where_to_put+1] = new_entries
    
    return [ aggregation ]


def find_remaining_services(hostspec, aggregation):
    tags, all_services = g_user_cache["services"][hostspec]
    all_services = set(all_services)
    for site, host, service in find_all_leaves(aggregation):
        if (site, host) == hostspec:
            all_services.discard(service)
    remaining = list(all_services)
    remaining.sort()
    return [ ( [hostspec], hostspec, service ) for service in remaining ]


# Helper function that finds all occurrances of a variable
# enclosed with $ and $. Returns a list of positions.
def find_variables(pattern, varname):
    found = []
    start = 0
    while True:
        pos = pattern.find('$' + varname + '$', start)
        if pos >= 0:
            found.append(pos)
            start = pos + 1
        else:
            return found

# replace variables in a string
def subst_vars(pattern, arginfo):
    for name, value in arginfo.items():
        if type(pattern) in [ str, unicode ]:
            pattern = pattern.replace('$'+name+'$', value)
    return pattern

def match_host_tags(have_tags, required_tags):
    for tag in required_tags:
        if tag.startswith('!'):
            negate = True
            tag = tag[1:]
        else:
            negate = False
        has_it = tag in have_tags
        if has_it == negate:
            return False
    return True

def compile_leaf_node(host_re, service_re = config.HOST_STATE):
    found = []
    honor_site = SITE_SEP in host_re

    # TODO: If we already know the host we deal with, we could avoid this loop
    for (site, hostname), (tags, services) in g_user_cache["services"].items():
        # If host ends with '|@all', we need to check host tags instead
        # of regexes.
        if host_re.endswith('|@all'):
            if not match_host_tags(host_re[:-5], tags):
                continue
        elif host_re != '@all':
            # For regex to have '$' anchor for end. Users might be surprised
            # to get a prefix match on host names. This is almost never what
            # they want. For services this is useful, however.
            if host_re.endswith("$"):
                anchored = host_re
            else:
                anchored = host_re + "$"

            # In order to distinguish hosts with the same name on different
            # sites we prepend the site to the host name. If the host specification
            # does not contain the site separator - though - we ignore the site
            # an match the rule for all sites.
            if honor_site:
                if not regex(anchored).match("%s%s%s" % (site, SITE_SEP, hostname)):
                    continue
            else:
                if not regex(anchored).match(hostname):
                    continue

            if service_re == config.HOST_STATE:
                found.append(([(site, hostname)], (site, hostname), config.HOST_STATE))

            elif service_re == config.REMAINING:
                found.append((config.REMAINING, (site, hostname)))

            else:
                for service in services:
                    if regex(service_re).match(service):
                        found.append(([(site, hostname)], (site, hostname), service))

    found.sort()
    return found


regex_cache = {}
def regex(r):
    rx = regex_cache.get(r)
    if rx:
        return rx
    try:
        rx = re.compile(r)
    except Exception, e:
        raise MKConfigError("Invalid regular expression '%s': %s" % (r, e))
    regex_cache[r] = rx
    return rx



#     _____                     _   _             
#    | ____|_  _____  ___ _   _| |_(_) ___  _ __  
#    |  _| \ \/ / _ \/ __| | | | __| |/ _ \| '_ \ 
#    | |___ >  <  __/ (__| |_| | |_| | (_) | | | |
#    |_____/_/\_\___|\___|\__,_|\__|_|\___/|_| |_|
#                                                 

# Get all status information we need for the aggregation from
# a known lists of lists (list of site/host pairs)
def get_status_info(required_hosts):

    # Query each site only for hosts that that site provides
    site_hosts = {}
    for site, host in required_hosts:
        hosts = site_hosts.get(site)
        if hosts == None:
            site_hosts[site] = [host]
        else:
            hosts.append(host)

    tuples = []
    for site, hosts in site_hosts.items():
        filter = ""
        for host in hosts:
            filter += "Filter: name = %s\n" % host
        if len(hosts) > 1:
            filter += "Or: %d\n" % len(hosts)
        data = html.live.query(
                "GET hosts\n"
                "Columns: name state plugin_output services_with_info\n"
                + filter)
        tuples += [((site, e[0]), e[1:]) for e in data]

    return dict(tuples)

# This variant of the function is configured not with a list of
# hosts but with a livestatus filter header and a list of columns
# that need to be fetched in any case
def get_status_info_filtered(filter_header, only_sites, limit, add_columns):
    columns = [ "name", "state", "plugin_output", "services_with_info" ] + add_columns

    html.live.set_only_sites(only_sites)
    html.live.set_prepend_site(True)
    data = html.live.query(
            "GET hosts\n" +
            "Columns: " + (" ".join(columns)) + "\n" +
            filter_header)
    html.live.set_prepend_site(False)
    html.live.set_only_sites(None)

    headers = [ "site" ] + columns
    rows = [ dict(zip(headers, row)) for row in data]
    return rows

# Execution of the trees. Returns a tree object reflecting
# the states of all nodes
def execute_tree(tree, status_info = None):
    if status_info == None:
        required_hosts = tree[0]
        status_info = get_status_info(required_hosts)
    return execute_node(tree, status_info)

def execute_node(node, status_info):
    # Each returned node consists of
    # (state, assumed_state, name, output, required_hosts, funcname, subtrees)
    # For leaf-nodes, subtrees is None
    if len(node) == 3:
        return execute_leaf_node(node, status_info) + (node[0], None, None,)
    else:
        return execute_inter_node(node, status_info)


def execute_leaf_node(node, status_info):
    required_hosts, (site, host), service = node
    status = status_info.get((site, host))
    if status == None:
        return (MISSING, None, None, "Host %s not found" % host) 

    host_state, host_output, service_state = status
    if service != config.HOST_STATE:
        key = (site, host, service)
    else:
        key = (site, host)
    assumed_state = g_assumptions.get(key)

    if service == config.HOST_STATE:
        aggr_state = {0:OK, 1:CRIT, 2:UNKNOWN}[host_state]
        return (aggr_state, assumed_state, None, host_output)

    else:
        for entry in service_state:
            if entry[0] == service:
                state, has_been_checked, plugin_output = entry[1:]
                if has_been_checked == 0:
                    return (PENDING, assumed_state, service, "This service has not been checked yet")
                else:
                    return (state, assumed_state, service, plugin_output)
        return (MISSING, assumed_state, service, "This host has no such service")


def execute_inter_node(node, status_info):
    required_hosts, title, funcspec, nodes = node
    parts = funcspec.split('!')
    funcname = parts[0]
    funcargs = parts[1:]
    func = config.aggregation_functions.get(funcname)
    if not func:
        raise MKConfigError("Undefined aggregation function '%s'. Available are: %s" % 
                (funcname, ", ".join(config.aggregation_functions.keys())))

    node_states = []
    assumed_node_states = []
    one_assumption = False
    for n in nodes:
        node_state = execute_node(n, status_info)
        node_states.append(node_state)
        assumed_state = node_state[1]
        if assumed_state != None:
            assumed_node_states.append((node_state[1],) + node_state[1:])
            one_assumption = True
        else:
            assumed_node_states.append(node_state)

    state, output = func(*([node_states] + funcargs))
    if one_assumption:
        assumed_state, output = func(*([assumed_node_states] + funcargs))
    else:
        assumed_state = None
    return (state, assumed_state, title, output, 
            required_hosts, funcspec, node_states )
    

#       _                      _____                 _   _                 
#      / \   __ _  __ _ _ __  |  ___|   _ _ __   ___| |_(_) ___  _ __  ___ 
#     / _ \ / _` |/ _` | '__| | |_ | | | | '_ \ / __| __| |/ _ \| '_ \/ __|
#    / ___ \ (_| | (_| | | _  |  _|| |_| | | | | (__| |_| | (_) | | | \__ \
#   /_/   \_\__, |\__, |_|(_) |_|   \__,_|_| |_|\___|\__|_|\___/|_| |_|___/
#           |___/ |___/                                                    

# Function for sorting states. Pending should be slightly
# worst then OK. CRIT is worse than UNKNOWN.
def state_weight(s):
    if s == CRIT:
        return 10.0
    elif s == PENDING:
        return 0.5
    else:
        return float(s)

def x_best_state(l, x):
    ll = [ (state_weight(s), s) for s in l ]
    ll.sort()
    if x < 0:
        ll.reverse()
    n = abs(x)
    if len(ll) < n:
        n = len(ll)
        
    return ll[n-1][1]

def aggr_nth_state(nodes, n, worst_state):
    states = []
    problems = []
    for node in nodes:
        states.append(node[0])
        if node[0] != OK:
            problems.append(node[1])
    state = x_best_state(states, n)
    if state_weight(state) > state_weight(worst_state):
        state = worst_state # limit to worst state

    if len(problems) > 0:
        return state, "%d problems" % len(problems)
    else:
        return state, ""

def aggr_worst(nodes, n = 1, worst_state = CRIT):
    return aggr_nth_state(nodes, -int(n), int(worst_state))

def aggr_best(nodes, n = 1, worst_state = CRIT):
    return aggr_nth_state(nodes, int(n), int(worst_state))

config.aggregation_functions["worst"] = aggr_worst 
config.aggregation_functions["best"]  = aggr_best

import re

def aggr_running_on(nodes, regex):
    first_check = nodes[0]

    # extract hostname we run on
    mo = re.match(regex, first_check[3])

    # if not found, then do normal aggregation with 'worst'
    if not mo or len(mo.groups()) == 0:
        state, text = aggregation_functions['worst'](nodes[1:])
        return state, text + ", running nowhere"

    running_on = mo.groups()[0]
    for host_node in nodes[1:]:
        if host_node[2] == running_on:
            return host_node[0], (host_node[3] + ", running on %s" % running_on)

    # host we run on not found. Strange...
    return 3, "running on unknown host '%s'" % running_on

config.aggregation_functions['running_on'] = aggr_running_on


#      ____                       
#     |  _ \ __ _  __ _  ___  ___ 
#     | |_) / _` |/ _` |/ _ \/ __|
#     |  __/ (_| | (_| |  __/\__ \
#     |_|   \__,_|\__, |\___||___/
#                 |___/           

# Just for debugging
def page_debug(h):
    global html
    html = h
    compile_forest(html.req.user)
    
    html.header("BI Debug")
    render_forest()
    html.footer()


# Just for debugging, as well
def page_all(h):
    global html
    html = h
    html.header("All")
    compile_forest(html.req.user)
    load_assumptions()
    for group, trees in g_user_cache["forest"].items():
        html.write("<h2>%s</h2>" % group)
        for inst_args, tree in trees:
            state = execute_tree(tree)
            debug(state)
    html.footer()


def ajax_set_assumption(h):
    global html
    html = h
    site = html.var("site")
    host = html.var("host")
    service = html.var("service")
    if service:
        key = (site, host, service)
    else:
        key = (site, host)
    state = html.var("state")
    load_assumptions()
    if state == 'none':
        del g_assumptions[key]
    else:
        g_assumptions[key] = int(state)
    save_assumptions()


def ajax_save_treestate(h):
    global html
    html = h
    path_id = html.var("path")
    current_ex_level, path = path_id.split(":", 1)
    current_ex_level = int(current_ex_level)
    state = html.var("state") == "open"
    saved_ex_level, treestate = load_treestate()
    if saved_ex_level != current_ex_level:
        treestate = {}
    treestate[path] = state
    save_treestate(current_ex_level, treestate)


#    ____        _                                          
#   |  _ \  __ _| |_ __ _ ___  ___  _   _ _ __ ___ ___  ___ 
#   | | | |/ _` | __/ _` / __|/ _ \| | | | '__/ __/ _ \/ __|
#   | |_| | (_| | || (_| \__ \ (_) | |_| | | | (_|  __/\__ \
#   |____/ \__,_|\__\__,_|___/\___/ \__,_|_|  \___\___||___/
#                                                           

def create_aggregation_row(tree, status_info = None):
    state = execute_tree(tree, status_info)
    eff_state = state[0]
    if state[1] != None:
        eff_state = state[1]
    else:
        eff_state = state[0]
    return {
        "aggr_tree"            : tree,
        "aggr_treestate"       : state,
        "aggr_state"           : state[0],  # state disregarding assumptions
        "aggr_assumed_state"   : state[1],  # is None, if no assumptions are done
        "aggr_effective_state" : eff_state, # is assumed_state, if there are assumptions, else real state
        "aggr_name"            : state[2],
        "aggr_output"          : state[3],
        "aggr_hosts"           : state[4],
        "aggr_function"        : state[5],
    }

def table(h, columns, add_headers, only_sites, limit, filters):
    global html
    html = h
    compile_forest(html.req.user)
    load_assumptions() # user specific, always loaded
    # Hier müsste man jetzt die Filter kennen, damit man nicht sinnlos
    # alle Aggregationen berechnet.
    rows = []
    # Apply group filter. This is important for performance. We 
    # must not compute any aggregations from other groups and filter 
    # later out again.
    only_group = None
    only_service = None
    
    for filter in filters:
        if filter.name == "aggr_group":
            only_group = filter.selected_group()
        elif filter.name == "aggr_service":
            only_service = filter.service_spec()

    # TODO: Optimation of affected_hosts filter!

    if only_service:
        affected = g_user_cache["affected_services"].get(only_service)
        if affected == None:
            items = []
        else:
            by_groups = {}
            for group, aggr in affected:
                entries = by_groups.get(group, [])
                entries.append(aggr)
                by_groups[group] = entries
            items = by_groups.items()

    else:
        items = g_user_cache["forest"].items()

    for group, trees in items:
        if only_group not in [ None, group ]:
            continue

        for tree in trees:
            row = create_aggregation_row(tree)
            row["aggr_group"] = group
            rows.append(row)
            if not html.check_limit(rows, limit):
                return rows
    return rows
        

# Table of all host aggregations, i.e. aggregations using data from exactly one host
def host_table(h, columns, add_headers, only_sites, limit, filters):
    global html
    html = h
    compile_forest(html.req.user)
    load_assumptions() # user specific, always loaded

    # Create livestatus filter for filtering out hosts. We can
    # simply use all those filters since we have a 1:n mapping between
    # hosts and host aggregations
    filter_code = ""
    for filt in filters: 
        header = filt.filter("bi_host_aggregations")
        if not header.startswith("Sites:"):
            filter_code += header

    host_columns = filter(lambda c: c.startswith("host_"), columns)
    hostrows = get_status_info_filtered(filter_code, only_sites, limit, host_columns)
    # if limit:
    #     views.check_limit(hostrows, limit)

    rows = []
    # Now compute aggregations of these hosts
    for hostrow in hostrows:
        site = hostrow["site"]
        host = hostrow["name"]
        status_info = { (site, host) : [ hostrow["state"], hostrow["plugin_output"], hostrow["services_with_info"] ] }
        for group, aggregation in g_user_cache["host_aggregations"].get((site, host), []):
            row = hostrow.copy()
            row.update(create_aggregation_row(aggregation, status_info))
            row["aggr_group"] = group
            rows.append(row)
            if not html.check_limit(rows, limit):
                return rows

    return rows


#     _   _      _                     
#    | | | | ___| |_ __   ___ _ __ ___ 
#    | |_| |/ _ \ | '_ \ / _ \ '__/ __|
#    |  _  |  __/ | |_) |  __/ |  \__ \
#    |_| |_|\___|_| .__/ \___|_|  |___/
#                 |_|                  

def debug(x):
    import pprint
    p = pprint.pformat(x)
    html.write("<pre>%s</pre>\n" % p)

def load_assumptions():
    global g_assumptions
    g_assumptions = config.load_user_file("bi_assumptions", {})

def save_assumptions():
    config.save_user_file("bi_assumptions", g_assumptions)

def load_treestate():
    return config.load_user_file("bi_treestate", (None, {}))

def save_treestate(current_ex_level, treestate):
    config.save_user_file("bi_treestate", (current_ex_level, treestate))

def status_tree_depth(tree):
    nodes = tree[6]
    if nodes == None:
        return 1
    else:
        maxdepth = 0
        for node in nodes:
            maxdepth = max(maxdepth, status_tree_depth(node))
        return maxdepth + 1

def is_part_of_aggregation(h, what, site, host, service):
    global html
    html = h
    compile_forest(html.req.user)
    if what == "host":
        return (site, host) in g_user_cache["affected_hosts"]
    else:
        return (site, host, service) in g_user_cache["affected_services"]

