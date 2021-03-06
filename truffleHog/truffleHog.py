#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import
import shutil
import sys
import math
import datetime
import argparse
import uuid
import hashlib
import tempfile
import os
import re
import json
import stat

from git import Repo
from git import NULL_TREE
from git import GitCommandError, InvalidGitRepositoryError
from truffleHogRegexes.regexChecks import regexes


def main():
    parser = argparse.ArgumentParser(description='Find secrets hidden in the depths of git.')
    parser.add_argument('--json', dest="output_json", action="store_true", help="Output in JSON")
    parser.add_argument("--regex", dest="do_regex", action="store_true", help="Enable high signal regex checks")
    parser.add_argument("--rules", dest="rules", help="Ignore default regexes and source from json list file")
    parser.add_argument("--entropy", dest="do_entropy", help="Enable entropy checks")
    parser.add_argument("--since_commit", dest="since_commit", help="Only scan from a given commit hash")
    parser.add_argument("--max_depth", dest="max_depth",
                        help="The max commit depth to go back when searching for secrets")
    parser.add_argument("--branch", dest="branch", help="Name of the branch to be scanned")
    parser.add_argument('-i', '--include_paths', type=argparse.FileType('r'), metavar='INCLUDE_PATHS_FILE',
                        help='File with regular expressions (one per line), at least one of which must match a Git '
                             'object path in order for it to be scanned; lines starting with "#" are treated as '
                             'comments and are ignored. If empty or not provided (default), all Git object paths are '
                             'included unless otherwise excluded via the --exclude_paths option.')
    parser.add_argument('-x', '--exclude_paths', type=argparse.FileType('r'), metavar='EXCLUDE_PATHS_FILE',
                        help='File with regular expressions (one per line), none of which may match a Git object path '
                             'in order for it to be scanned; lines starting with "#" are treated as comments and are '
                             'ignored. If empty or not provided (default), no Git object paths are excluded unless '
                             'effectively excluded via the --include_paths option.')
    parser.add_argument('-e', '--entropy-exclude-regex', type=argparse.FileType('r'), metavar='ENTROPY_EXCLUDE_RE_FILE',
                        help='File with regular expressions (one perline).'
                             ' The line matching one of these regex will be excluded from high entropy testing.')
    parser.add_argument("--repo_path", type=str, dest="repo_path",
                        help="Path to the cloned repo. If provided, git_url will not be used")
    parser.add_argument("--cleanup", dest="cleanup", action="store_true", help="Clean up all temporary result files")
    parser.add_argument('git_url', type=str, help='URL for secret searching')
    parser.set_defaults(regex=False)
    parser.set_defaults(rules={})
    parser.set_defaults(max_depth=1000000)
    parser.set_defaults(since_commit=None)
    parser.set_defaults(entropy=True)
    parser.set_defaults(branch=None)
    parser.set_defaults(repo_path=None)
    parser.set_defaults(cleanup=False)
    args = parser.parse_args()
    rules = {}
    if args.rules:
        try:
            with open(args.rules, "r") as ruleFile:
                rules = json.loads(ruleFile.read())
                for rule in rules:
                    rules[rule] = re.compile(rules[rule])
        except (IOError, ValueError) as e:
            raise Exception("Error reading rules file. got:", e.value)
        for regex in dict(regexes):
            del regexes[regex]
        for regex in rules:
            regexes[regex] = rules[regex]
    do_entropy = str2bool(args.do_entropy)

    # read & compile inclusion/exclusion patterns
    path_inclusions = []
    path_exclusions = []
    entropy_regex_exclusions = []
    if args.include_paths:
        for pattern in set(lead[:-1].lstrip() for lead in args.include_paths):
            if pattern and not pattern.startswith('#'):
                path_inclusions.append(re.compile(pattern))
    if args.exclude_paths:
        for pattern in set(lead[:-1].lstrip() for lead in args.exclude_paths):
            if pattern and not pattern.startswith('#'):
                path_exclusions.append(re.compile(pattern))
    if args.entropy_exclude_regex:
        for pattern in set(lead[:-1].lstrip() for lead in args.entropy_exclude_regex):
            if pattern and not pattern.startswith('#'):
                entropy_regex_exclusions.append(re.compile(pattern))

    output = find_strings(args.git_url, args.since_commit, args.max_depth, args.output_json, args.do_regex, do_entropy,
                          surpress_output=False, branch=args.branch, repo_path=args.repo_path,
                          path_inclusions=path_inclusions, path_exclusions=path_exclusions,
                          entropy_regex_exclusions=entropy_regex_exclusions)
    if args.cleanup:
        clean_up(output)
    if output["found_issues"]:
        sys.exit(1)
    else:
        sys.exit(0)


def str2bool(v):
    if v is None:
        return True
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


BASE64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
HEX_CHARS = "1234567890abcdefABCDEF"


def del_rw(action, name, exc):
    os.chmod(name, stat.S_IWRITE)
    os.remove(name)


def shannon_entropy(data, iterator):
    """
    Borrowed from http://blog.dkbza.org/2007/05/scanning-data-for-entropy-anomalies.html
    """
    if not data:
        return 0
    entropy = 0
    for x in iterator:
        p_x = float(data.count(x)) / len(data)
        if p_x > 0:
            entropy += - p_x * math.log(p_x, 2)
    return entropy


def get_strings_of_set(word, char_set, threshold=20):
    count = 0
    letters = ""
    strings = []
    for char in word:
        if char in char_set:
            letters += char
            count += 1
        else:
            if count > threshold:
                strings.append(letters)
            letters = ""
            count = 0
    if count > threshold:
        strings.append(letters)
    return strings


class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def clone_git_repo(git_url):
    project_path = tempfile.mkdtemp()
    try:
        Repo.clone_from(git_url, project_path)
    except GitCommandError as e:
        if re.search("repository.* *.does not exist", e.stderr):
            exit(bcolors.FAIL + "Error: Git repository does not exist" + bcolors.ENDC)
        else:
            exit(bcolors.FAIL + "Error: Git command error: \n" + bcolors.ENDC + str(e))
    return project_path


def print_results(print_json, issue):
    commit_time = issue['date']
    branch_name = issue['branch']
    prev_commit = issue['commit']
    printable_diff = issue['print_diff']
    commit_hash = issue['commit_hash']
    reason = issue['reason']
    path = issue['path']

    if print_json:
        print(json.dumps(issue, sort_keys=True))
    else:
        print("~~~~~~~~~~~~~~~~~~~~~")
        reason = "{}Reason: {}{}".format(bcolors.OKGREEN, reason, bcolors.ENDC)
        print(reason)
        date_str = "{}Date: {}{}".format(bcolors.OKGREEN, commit_time, bcolors.ENDC)
        print(date_str)
        hash_str = "{}Hash: {}{}".format(bcolors.OKGREEN, commit_hash, bcolors.ENDC)
        print(hash_str)
        file_path = "{}file_path: {}{}".format(bcolors.OKGREEN, path, bcolors.ENDC)
        print(file_path)

        if sys.version_info >= (3, 0):
            branch_str = "{}Branch: {}{}".format(bcolors.OKGREEN, branch_name, bcolors.ENDC)
            print(branch_str)
            commit_str = "{}Commit: {}{}".format(bcolors.OKGREEN, prev_commit, bcolors.ENDC)
            print(commit_str)
            print(printable_diff)
        else:
            branch_str = "{}Branch: {}{}".format(bcolors.OKGREEN, branch_name.encode('utf-8'), bcolors.ENDC)
            print(branch_str)
            commit_str = "{}Commit: {}{}".format(bcolors.OKGREEN, prev_commit.encode('utf-8'), bcolors.ENDC)
            print(commit_str)
            print(printable_diff.encode('utf-8'))
        print("~~~~~~~~~~~~~~~~~~~~~")


def is_exclude_line(line, entropy_regex_exclusions):
    if is_no_trufflehog_line(line):
        return True
    for regex_exclusion in entropy_regex_exclusions:
        if regex_exclusion.search(line):
            return True
    return False


def is_no_trufflehog_line(line):
    return "no_trufflehog" in line


def find_entropy(printable_diff, commit_time, branch_name, prev_commit, blob, entropy_regex_exclusions):
    strings_found = []
    lines = printable_diff.split("\n")

    for line in lines:
        if is_exclude_line(line, entropy_regex_exclusions):
            continue
        for word in line.split():
            base64_strings = get_strings_of_set(word, BASE64_CHARS)
            hex_strings = get_strings_of_set(word, HEX_CHARS)
            for string in base64_strings:
                b64_entropy = shannon_entropy(string, BASE64_CHARS)
                if b64_entropy > 4.5:
                    strings_found.append(string)
                    printable_diff = printable_diff.replace(string, bcolors.WARNING + string + bcolors.ENDC)
            for string in hex_strings:
                hex_entropy = shannon_entropy(string, HEX_CHARS)
                if hex_entropy > 3:
                    strings_found.append(string)
                    printable_diff = printable_diff.replace(string, bcolors.WARNING + string + bcolors.ENDC)
    entropic_diff = None
    if len(strings_found) > 0:
        entropic_diff = {'date': commit_time, 'path': blob.b_path if blob.b_path else blob.a_path,
                         'branch': branch_name, 'commit': prev_commit.message,
                         'diff': blob.diff.decode('utf-8', errors='replace'), 'strings_found': strings_found,
                         'print_diff': printable_diff, 'commit_hash': prev_commit.hexsha, 'reason': "High Entropy"}
    return entropic_diff


def regex_check(printable_diff, commit_time, branch_name, prev_commit, blob, commit_hash, custom_regexes=None):
    if custom_regexes is None:
        custom_regexes = {}
    if custom_regexes:
        secret_regexes = custom_regexes
    else:
        secret_regexes = regexes
    regex_matches = []
    found_diff = None
    for key in secret_regexes:
        found_strings = secret_regexes[key].findall(printable_diff)
        for found_string in found_strings:
            found_diff = printable_diff.replace(printable_diff, bcolors.WARNING + found_string + bcolors.ENDC)
        if found_strings:
            found_regex = {'date': commit_time, 'path': blob.b_path if blob.b_path else blob.a_path,
                           'branch': branch_name, 'commit': prev_commit.message,
                           'diff': blob.diff.decode('utf-8', errors='replace'), 'strings_found': found_strings,
                           'print_diff': found_diff, 'reason': key, 'commit_hash': prev_commit.hexsha}
            regex_matches.append(found_regex)
    return regex_matches


def diff_worker(diff, prev_commit, branch_name, commit_hash, custom_regexes, do_entropy, do_regex,
                print_json, surpress_output, path_inclusions, path_exclusions, entropy_regex_exclusions):
    issues = []
    for blob in diff:
        printable_diff = blob.diff.decode('utf-8', errors='replace')
        if printable_diff.startswith("Binary files"):
            continue
        if not path_included(blob, path_inclusions, path_exclusions):
            continue
        commit_time = datetime.datetime.fromtimestamp(prev_commit.committed_date).strftime('%Y-%m-%d %H:%M:%S')
        found_issues = []
        if do_entropy:
            entropic_diff = find_entropy(printable_diff, commit_time, branch_name, prev_commit, blob, entropy_regex_exclusions)
            if entropic_diff:
                found_issues.append(entropic_diff)
        if do_regex:
            found_regexes = regex_check(printable_diff, commit_time, branch_name, prev_commit, blob, commit_hash,
                                        custom_regexes)
            found_issues += found_regexes
        if not surpress_output:
            for foundIssue in found_issues:
                print_results(print_json, foundIssue)
        issues += found_issues
    return issues


def handle_results(output, output_dir, found_issues):
    for foundIssue in found_issues:
        result_path = os.path.join(output_dir, str(uuid.uuid4()))
        with open(result_path, "w+") as result_file:
            result_file.write(json.dumps(foundIssue))
        output["found_issues"].append(result_path)
    return output


def path_included(blob, include_patterns=None, exclude_patterns=None):
    """Check if the diff blob object should included in analysis.

    If defined and non-empty, `include_patterns` has precedence over `exclude_patterns`, such that a blob that is not
    matched by any of the defined `include_patterns` will be excluded, even when it is not matched by any of the defined
    `exclude_patterns`. If either `include_patterns` or `exclude_patterns` are undefined or empty, they will have no
    effect, respectively. All blobs are included by this function when called with default arguments.

    :param blob: a Git diff blob object
    :param include_patterns: iterable of compiled regular expression objects; when non-empty, at least one pattern must
     match the blob object for it to be included; if empty or None, all blobs are included, unless excluded via
     `exclude_patterns`
    :param exclude_patterns: iterable of compiled regular expression objects; when non-empty, _none_ of the patterns may
     match the blob object for it to be included; if empty or None, no blobs are excluded if not otherwise
     excluded via `include_patterns`
    :return: False if the blob is _not_ matched by `include_patterns` (when provided) or if it is matched by
    `exclude_patterns` (when provided), otherwise returns True
    """
    path = blob.b_path if blob.b_path else blob.a_path
    if include_patterns and not any(p.match(path) for p in include_patterns):
        return False
    if exclude_patterns and any(p.match(path) for p in exclude_patterns):
        return False
    return True


def find_strings(git_url, since_commit=None, max_depth=1000000, print_json=False, do_regex=False, do_entropy=True,
                 surpress_output=True,
                 custom_regexes=None, branch=None, repo_path=None, path_inclusions=None, path_exclusions=None,
                 entropy_regex_exclusions=None):
    if custom_regexes is None:
        custom_regexes = {}
    if entropy_regex_exclusions is None:
        entropy_regex_exclusions = []
    output = {"found_issues": []}
    if repo_path:
        project_path = repo_path
    else:
        project_path = clone_git_repo(git_url)
    try:
        repo = Repo(project_path)
    except InvalidGitRepositoryError:
        exit(bcolors.BOLD + bcolors.FAIL + "Error: Invalid Git repository" + bcolors.ENDC)
    already_searched = set()
    output_dir = tempfile.mkdtemp()

    if branch:
        branches = repo.remotes.origin.fetch(branch)
    else:
        branches = repo.remotes.origin.fetch()

    for remote_branch in branches:
        since_commit_reached = False
        branch_name = remote_branch.name
        prev_commit, curr_commit = None, None
        commit_hash = None
        for curr_commit in repo.iter_commits(branch_name, max_count=max_depth):
            commit_hash = curr_commit.hexsha
            if commit_hash == since_commit:
                since_commit_reached = True
            if since_commit and since_commit_reached:
                prev_commit = curr_commit
                continue
            # if not prev_commit, then curr_commit is the newest commit. And we have nothing to diff with.
            # But we will diff the first commit with NULL_TREE here to check the oldest code.
            # In this way, no commit will be missed.
            diff_hash = hashlib.sha3_512((str(prev_commit) + str(curr_commit)).encode('utf-8')).digest()
            if not prev_commit:
                prev_commit = curr_commit
                continue
            elif diff_hash in already_searched:
                prev_commit = curr_commit
                continue
            else:
                diff = prev_commit.diff(curr_commit, create_patch=True)
            # avoid searching the same diffs
            already_searched.add(diff_hash)
            found_issues = diff_worker(diff, prev_commit, branch_name, commit_hash, custom_regexes,
                                       do_entropy, do_regex, print_json, surpress_output, path_inclusions,
                                       path_exclusions, entropy_regex_exclusions)
            output = handle_results(output, output_dir, found_issues)
            prev_commit = curr_commit
        # Handling the first commit
        diff = curr_commit.diff(NULL_TREE, create_patch=True)
        found_issues = diff_worker(diff, prev_commit, branch_name, commit_hash, custom_regexes, do_entropy,
                                   do_regex, print_json, surpress_output, path_inclusions, path_exclusions,
                                   entropy_regex_exclusions)
        output = handle_results(output, output_dir, found_issues)
    output["project_path"] = project_path
    output["clone_uri"] = git_url
    output["issues_path"] = output_dir
    if not repo_path:
        shutil.rmtree(project_path, onerror=del_rw)
    return output


def clean_up(output):
    issues_path = output.get("issues_path", None)
    if issues_path and os.path.isdir(issues_path):
        shutil.rmtree(output["issues_path"])


if __name__ == "__main__":
    main()
