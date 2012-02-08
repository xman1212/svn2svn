""" SVN client functions """

from shell import run_svn
from errors import EmptySVNLog

import os
import time
import calendar
import operator

try:
    from xml.etree import cElementTree as ET
except ImportError:
    try:
        from xml.etree import ElementTree as ET
    except ImportError:
        try:
            import cElementTree as ET
        except ImportError:
            from elementtree import ElementTree as ET

_identity_table = "".join(map(chr, range(256)))
_forbidden_xml_chars = "".join(
    set(map(chr, range(32))) - set('\x09\x0A\x0D')
)


def strip_forbidden_xml_chars(xml_string):
    """
    Given an XML string, strips forbidden characters as per the XML spec.
    (these are all control characters except 0x9, 0xA and 0xD).
    """
    return xml_string.translate(_identity_table, _forbidden_xml_chars)


def svn_date_to_timestamp(svn_date):
    """
    Parse an SVN date as read from the XML output and return the corresponding
    timestamp.
    """
    # Strip microseconds and timezone (always UTC, hopefully)
    # XXX there are various ISO datetime parsing routines out there,
    # cf. http://seehuhn.de/comp/pdate
    date = svn_date.split('.', 2)[0]
    time_tuple = time.strptime(date, "%Y-%m-%dT%H:%M:%S")
    return calendar.timegm(time_tuple)

def parse_svn_info_xml(xml_string):
    """
    Parse the XML output from an "svn info" command and extract useful information
    as a dict.
    """
    d = {}
    xml_string = strip_forbidden_xml_chars(xml_string)
    tree = ET.fromstring(xml_string)
    entry = tree.find('.//entry')
    d['url'] = entry.find('url').text
    d['kind'] = entry.get('kind')
    d['revision'] = int(entry.get('revision'))
    d['repos_url'] = tree.find('.//repository/root').text
    d['repos_uuid'] = tree.find('.//repository/uuid').text
    d['last_changed_rev'] = int(tree.find('.//commit').get('revision'))
    author_element = tree.find('.//commit/author')
    if author_element is not None:
        d['last_changed_author'] = author_element.text
    d['last_changed_date'] = svn_date_to_timestamp(tree.find('.//commit/date').text)
    return d

def _get_kind(svn_repos_url, svn_path, svn_rev, action, paths):
    """
    Calculate the "kind"-type of a given URL in the SVN repo.
    """
    # By default, just do a simple "svn info" based on passed-in params.
    info_path = svn_path
    info_rev =  svn_rev
    if action == 'D':
        # For deletions, we can't do an "svn info" at this revision.
        # Need to trace ancestry backwards.
        parents = []
        for p in paths:
            # Build a list of any copy-from's in this log_entry that we're a child of.
            if p['copyfrom_revision'] and svn_path.startswith(p['path']):
                parents.append(p['path'])
        if parents:
            # Use the nearest copy-from'd parent
            parents.sort()
            parent = parents[len(parents)-1]
            for p in paths:
                if parent == p['path']:
                    info_path = p['copyfrom_path']
                    info_rev =  p['copyfrom_revision']
        else:
            # If no parent copy-from's, then we should be able to check this path in
            # the preceeding revision.
            info_rev -= 1
    info = get_svn_info(svn_repos_url+info_path, info_rev)
    return info['kind']

def parse_svn_log_xml(xml_string, svn_url_or_wc):
    """
    Parse the XML output from an "svn log" command and extract useful information
    as a list of dicts (one per log changeset).
    """
    l = []
    info = {}
    svn_repos_url = ""
    xml_string = strip_forbidden_xml_chars(xml_string)
    tree = ET.fromstring(xml_string)
    for entry in tree.findall('logentry'):
        d = {}
        d['revision'] = int(entry.get('revision'))
        if not info:
            info = get_svn_info(svn_url_or_wc, d['revision'])
            svn_repos_url = info['repos_url']
        # Some revisions don't have authors, most notably the first revision
        # in a repository.
        # logentry nodes targeting directories protected by path-based
        # authentication have no child nodes at all. We return an entry
        # in that case. Anyway, as it has no path entries, no further
        # processing will be made.
        author = entry.find('author')
        date = entry.find('date')
        msg = entry.find('msg')
        d['author'] = author is not None and author.text or "No author"
        if date is not None:
            d['date'] = svn_date_to_timestamp(date.text)
        else:
            d['date'] = None
        d['message'] = msg is not None and msg.text and msg.text.replace('\r\n', '\n').replace('\n\r', '\n').replace('\r', '\n') or ""
        paths = []
        for path in entry.findall('.//paths/path'):
            copyfrom_rev = path.get('copyfrom-rev')
            if copyfrom_rev:
                copyfrom_rev = int(copyfrom_rev)
            cur_path = path.text
            kind = path.get('kind')
            action = path.get('action')
            if kind == "":
                kind = _get_kind(svn_repos_url, cur_path, d['revision'], action, paths)
            assert (kind == 'file') or (kind == 'dir')
            paths.append({
                'path': path.text,
                'kind': kind,
                'action': action,
                'copyfrom_path': path.get('copyfrom-path'),
                'copyfrom_revision': copyfrom_rev,
            })
        # Sort paths (i.e. into hierarchical order), so that process_svn_log_entry()
        # can process actions in depth-first order.
        d['changed_paths'] = sorted(paths, key=operator.itemgetter('path'))
        revprops = []
        for prop in entry.findall('.//revprops/property'):
            revprops.append({ 'name': prop.get('name'), 'value': prop.text })
        d['revprops'] = revprops
        l.append(d)
    return l

def parse_svn_status_xml(xml_string, base_dir=None, ignore_externals=False):
    """
    Parse the XML output from an "svn status" command and extract useful info
    as a list of dicts (one per status entry).
    """
    if base_dir:
        base_dir = os.path.normcase(base_dir)
    l = []
    xml_string = strip_forbidden_xml_chars(xml_string)
    tree = ET.fromstring(xml_string)
    for entry in tree.findall('.//entry'):
        d = {}
        path = entry.get('path')
        if base_dir is not None:
            assert os.path.normcase(path).startswith(base_dir)
            path = path[len(base_dir):].lstrip('/\\')
        d['path'] = path
        wc_status = entry.find('wc-status')
        if wc_status.get('item') == 'external':
            if ignore_externals:
                continue
        status =   wc_status.get('item')
        revision = wc_status.get('revision')
        if status == 'external':
            d['type'] = 'external'
        elif revision is not None:
            d['type'] = 'normal'
        else:
            d['type'] = 'unversioned'
        d['status'] =   status
        d['revision'] = revision
        d['props'] =    wc_status.get('props')
        d['copied'] =   wc_status.get('copied')
        l.append(d)
    return l

def get_svn_info(svn_url_or_wc, rev_number=None):
    """
    Get SVN information for the given URL or working copy, with an optionally
    specified revision number.
    Returns a dict as created by parse_svn_info_xml().
    """
    args = ['info', '--xml']
    if rev_number is not None:
        args += ["-r", rev_number, svn_url_or_wc+"@"+str(rev_number)]
    else:
        args += [svn_url_or_wc]
    xml_string = run_svn(args, fail_if_stderr=True)
    return parse_svn_info_xml(xml_string)

def svn_checkout(svn_url, checkout_dir, rev_number=None):
    """
    Checkout the given URL at an optional revision number.
    """
    args = ['checkout', '-q']
    if rev_number is not None:
        args += ['-r', rev_number]
    args += [svn_url, checkout_dir]
    return run_svn(args)

def run_svn_log(svn_url_or_wc, rev_start, rev_end, limit, stop_on_copy=False, get_changed_paths=True, get_revprops=False):
    """
    Fetch up to 'limit' SVN log entries between the given revisions.
    """
    args = ['log', '--xml']
    if stop_on_copy:
        args += ['--stop-on-copy']
    if get_changed_paths:
        args += ['-v']
    if get_revprops:
        args += ['--with-all-revprops']
    url = str(svn_url_or_wc)
    args += ['-r', '%s:%s' % (rev_start, rev_end)]
    if not "@" in svn_url_or_wc:
        url = "%s@%s" % (svn_url_or_wc, str(max(rev_start, rev_end)))
    args += ['--limit', str(limit), url]
    xml_string = run_svn(args)
    return parse_svn_log_xml(xml_string, svn_url_or_wc)

def get_svn_status(svn_wc, quiet=False, no_recursive=False):
    """
    Get SVN status information about the given working copy.
    """
    # Ensure proper stripping by canonicalizing the path
    svn_wc = os.path.abspath(svn_wc)
    args = ['status', '--xml', '--ignore-externals']
    if quiet:
        args += ['-q']
    else:
        args += ['-v']
    if no_recursive:
        args += ['-N']
    xml_string = run_svn(args + [svn_wc])
    return parse_svn_status_xml(xml_string, svn_wc, ignore_externals=True)

def get_svn_versioned_files(svn_wc):
    """
    Get the list of versioned files in the SVN working copy.
    """
    contents = []
    for e in get_svn_status(svn_wc):
        if e['path'] and e['type'] == 'normal':
            contents.append(e['path'])
    return contents

def get_one_svn_log_entry(svn_url, rev_start, rev_end, stop_on_copy=False, get_changed_paths=True, get_revprops=False):
    """
    Get the first SVN log entry in the requested revision range.
    """
    entries = run_svn_log(svn_url, rev_start, rev_end, 1, stop_on_copy, get_changed_paths, get_revprops)
    if entries:
        return entries[0]
    raise EmptySVNLog("No SVN log for %s between revisions %s and %s" %
        (svn_url, rev_start, rev_end))

def get_first_svn_log_entry(svn_url, rev_start, rev_end, get_changed_paths=True):
    """
    Get the first log entry after (or at) the given revision number in an SVN branch.
    By default the revision number is set to 0, which will give you the log
    entry corresponding to the branch creaction.

    NOTE: to know whether the branch creation corresponds to an SVN import or
    a copy from another branch, inspect elements of the 'changed_paths' entry
    in the returned dictionary.
    """
    return get_one_svn_log_entry(svn_url, rev_start, rev_end, stop_on_copy=True, get_changed_paths=True)

def get_last_svn_log_entry(svn_url, rev_start, rev_end, get_changed_paths=True):
    """
    Get the last log entry before/at the given revision number in an SVN branch.
    By default the revision number is set to HEAD, which will give you the log
    entry corresponding to the latest commit in branch.
    """
    return get_one_svn_log_entry(svn_url, rev_end, rev_start, stop_on_copy=True, get_changed_paths=True)


log_duration_threshold = 10.0
log_min_chunk_length = 10
log_max_chunk_length = 10000

def iter_svn_log_entries(svn_url, first_rev, last_rev, stop_on_copy=False, get_changed_paths=True, get_revprops=False):
    """
    Iterate over SVN log entries between first_rev and last_rev.

    This function features chunked log fetching so that it isn't too nasty
    to the SVN server if many entries are requested.

    NOTE: This chunked log fetching *ONLY* works correctly on paths which
    are known to have existed unbroken in the SVN repository, e.g. /trunk.
    Chunked fetching breaks down if a path existed in earlier, then was
    deleted, and later was re-created. For example, if path was created in r5,
    then deleted in r1000, and then later re-created in r5000...
      svn log --stop-on-copy --limit 1 -r 1:50 "path/to/file"
        --> would yield r5, i.e. the _initial_ creation
      svn log --stop-on-copy --limit 1 -r 1:HEAD "path/to/file"
        --> would yield r5000, i.e. the _re-creation_
    In theory this might work if we always search "backwards", searching from
    the end going forward rather than forward going to the end...
    """
    if last_rev == "HEAD":
        info = get_svn_info(svn_url)
        last_rev = info['revision']
    cur_rev = first_rev
    chunk_length = log_min_chunk_length
    while cur_rev <= last_rev:
        start_t = time.time()
        stop_rev = min(last_rev, cur_rev + chunk_length)
        entries = run_svn_log(svn_url, cur_rev, stop_rev, chunk_length,
                              stop_on_copy, get_changed_paths, get_revprops)
        duration = time.time() - start_t
        if entries:
            for e in entries:
                if e['revision'] > last_rev:
                    break
                yield e
            if e['revision'] >= last_rev:
                break
            cur_rev = e['revision']+1
        else:
            cur_rev = int(stop_rev)+1
        # Adapt chunk length based on measured request duration
        if duration < log_duration_threshold:
            chunk_length = min(log_max_chunk_length, int(chunk_length * 2.0))
        elif duration > log_duration_threshold * 2:
            chunk_length = max(log_min_chunk_length, int(chunk_length / 2.0))


_svn_client_version = None

def get_svn_client_version():
    """
    Returns the SVN client version as a tuple.

    The returned tuple only contains numbers, non-digits in version string are
    silently ignored.
    """
    global _svn_client_version
    if _svn_client_version is None:
        raw = run_svn(['--version', '-q']).strip()
        _svn_client_version = tuple(map(int, [x for x in raw.split('.')
                                              if x.isdigit()]))
    return _svn_client_version
