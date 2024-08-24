import argparse
import configparser
import pathlib
import re
import tomllib
from html.parser import HTMLParser
from packaging.requirements import Requirement
from urllib.parse import urlparse

import importlib.resources
import importlib.metadata
import importlib.abc
import requests


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; x86)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/png,image/svg+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "DNT": "1",
    "Sec-GPC": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Priority": "u=0, i",
    "TE": "trailers",
}

class Regex:
    PYPI_PACKAGE_NAME = re.compile(r"[a-zA-Z](?:[a-zA-Z0-9]+|(?:\-|\.[a-zA-Z0-9]+))*")
    GITHUB_BRANCH = re.compile(r"<span class=\"Text-sc-17v1xeu-0 bOMzPg\">.*?>(?P<branch>.*?)</span>")
    GITHUB_TAG = re.compile(r"<a.*?href=\".*?/releases/tag/.*?>(?P<tag>.*?)</a>")
    GITHUB_VERSION_TAG = r"<a.*?href=\".*?/releases/tag/.*?>(?P<tag>.*?{version}.*?)</a>" 
    DEPENDENCIES = re.compile(r"(?:(?:install_requires|requires)\s*?=\s*?\[(?P<deps>(\s*.*?)+)\])")
    STRING = re.compile(r"[\"'](.*?)[\"']")
    DID_YOU_MEAN = re.compile(r"Did you mean '.*?>(?P<name>.*?)<.*?'\?")

class PyPIPackageHTMLParser(HTMLParser):
    def __init__(self, *, convert_charrefs: bool = True) -> None:
        super().__init__(convert_charrefs=convert_charrefs)
        self.capture = None
        self.package = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h1":
            if "class" in attrs and attrs["class"] == "package-header__name":
                self.capture = attrs["class"]
        elif tag == "p":
            if "class" in attrs:
                if attrs["class"] in ("package-header__date", "package-description__summary"):
                    self.capture = attrs["class"]
        elif self.capture and tag == "a":
            if self.capture == "Project links":
                self.package["Links"].append(attrs["href"].strip())
            elif self.capture == "Author:":
                self.package["Author-email"] = attrs["href"].strip()

    def handle_endtag(self, tag):
        if self.capture == "Project links":
            if tag == "ul":
                self.capture = None
            elif tag == "a":
                url = self.package["Links"].pop(-1)
                self.package["Links"].append((self.lastdata, url))

    def handle_data(self, data):
        self.lastdata = data.strip()
        if self.capture:
            if self.lasttag == "time":
                if self.capture == "package-header__date":
                    self.package["Release"] = self.lastdata
            elif self.lasttag == "strong":
                if self.capture == "Requires:":
                    self.package["Requires"] = self.lastdata
                if self.capture == "Author:":
                    if self.lastdata:
                        self.package["Author"] = self.lastdata
            elif self.lasttag == "a":
                if self.capture == "Author:":
                    self.package["Author"] = self.lastdata
            else:
                if self.capture == "package-header__name":
                    self.package["Name"], self.package["Version"] = self.lastdata.split(" ")
                elif self.capture == "package-description__summary":
                    self.package["Summary"] = self.lastdata
                elif self.capture == "License:":
                    self.package["License"] = self.lastdata
            if not (self.capture == "Author:" and self.lasttag == "strong" and not self.lastdata) and \
               not (self.capture == "Project links"):
                self.capture = None
        else:
            if self.lastdata in ("Project links", "License:", "Author:", "Requires:"):
                self.capture = self.lastdata
                if self.lastdata == "Project links":
                    self.package["Links"] = []

class SnykAdvisorHTMLParser(HTMLParser):
    def __init__(self, *, convert_charrefs: bool = True) -> None:
        super().__init__(convert_charrefs=convert_charrefs)
        self.lastdata = self.capture = None
        self.spans = ("Package Health Score", "Popularity", "GitHub Stars", "Forks",
                      "Maintenance", "Open Issues", "Open PR", "Last Release",
                      "Last Commit", "Security", "License", "Security Policy", 
                      "Community", "Readme", "Contributing.md", "Code of Conduct",
                      "Contributors", "Funding", "Python Versions Compatibility",
                      "Age", "Latest Release", "Dependencies", "Versions",
                      "Maintainers", "Wheels")
        self.progress = {title: False for title in self.spans}
        self.package_health = {}
    
    def handle_starttag(self, tag, attrs):
        ...

    def handle_endtag(self, tag):
        ...

    def handle_data(self, data):
        data = data.strip()
        if not self.capture:
            if data in self.spans and not self.progress[data]:
                self.capture = data
                self.progress[data] = True
            elif data == "(Latest)":
                self.package_health["Latest Version"] = self.lastdata
        else:
            if data:
                self.package_health[self.capture.title()] = data
                self.capture = None
        self.lastdata = data

def is_valid_package_name(name: str) -> bool:
    if re.match(r"[a-zA-Z](?:[a-zA-Z0-9]+|(?:\-|\.[a-zA-Z0-9]+))*", name):
        return True
    return False

def confirm(message: str = "", question = "Are you sure?") -> bool:
    answer = input(f"{question} {message + ' ' if message else message}(y/n): ")
    if answer.strip() in ("y", "Y"):
        return True
    return False

def did_you_mean(session: requests.Session, query: str) -> str:
    response = session.get(url="https://pypi.org/search/", params={"q": query}, headers=HEADERS)
    content = response.content.decode("utf-8")
    if (match_ := re.search(Regex.DID_YOU_MEAN, content)):
        if confirm(question=f"Did you mean {repr(match_["name"])}?"):
            return match_["name"]
    return query

def search_dependencies(session: requests.Session, package: dict[str, str], version: str):
    source = source_url = None
    if "Links" in package:
        for _, url in package["Links"]:
            parsed = urlparse(url)
            if parsed.scheme == "https" and parsed.netloc == "github.com":
                path = "/".join(parsed.path.split("/")[:3])
                source = parsed._replace(params="", query="", path=path, fragment="")
                source_url = source.geturl()
                break

    if source:
        branch = tag = None
        if not version:
            response = session.get(source_url, headers=HEADERS)
            content = response.content.decode("utf-8")
            branch = re.search(Regex.GITHUB_BRANCH, content)["branch"]
        else:
            response = session.get(f"{source_url}/tags", headers=HEADERS)
            content = response.content.decode("utf-8")
            compiled_pattern = re.compile(Regex.GITHUB_VERSION_TAG.format(version=version))
            if tag := re.search(compiled_pattern, content):
                tag = tag["tag"]
            else:
                last_tag = None
                while not tag or not last_tag:
                    last_tag = re.findall(Regex.GITHUB_TAG, content)[-1]
                    response = session.get(f"{source_url}/tags", params={"after": last_tag}, headers=HEADERS)
                    content = response.content.decode("utf-8")
                    tag = re.search(compiled_pattern, content)
                if tag:
                    tag = tag["tag"]

        source_raw_url = f"https://raw.githubusercontent.com{source.path}/{tag if tag else branch}"
        dependencies, optional_dependencies = set(), set()

        response = session.get(f"{source_raw_url}/setup.cfg", headers=HEADERS)
        if response.status_code != 404:
            content = response.content.decode("utf-8")
            config = configparser.ConfigParser()
            config.read_string(content)
            for section in config.sections():
                if "requires-dist" in config[section]:
                    dependencies.update(config[section]["requires-dist"].split())
                elif "install_requires" in config[section]:
                    dependencies.update(config[section]["install_requires"].split())
        
        if not dependencies:
            response = session.get(f"{source_raw_url}/pyproject.toml", headers=HEADERS)
            if response.status_code != 404:
                content = response.content.decode("utf-8")
                toml_config = tomllib.loads(content)
                if "project" in toml_config:
                    if "dependencies" in toml_config["project"]:
                        dependencies.update(toml_config["project"]["dependencies"])
                    if "optional-dependencies" in toml_config["project"]:
                        for option in toml_config["project"]["optional-dependencies"]:
                            deps = toml_config["project"]["optional-dependencies"][option]
                            optional_dependencies.add((option, tuple(deps)))
        
        if not dependencies:
            response = session.get(f"{source_raw_url}/setup.py", headers=HEADERS)
            if response.status_code != 404:
                content = response.content.decode("utf-8")
                if (possible_deps := re.search(Regex.DEPENDENCIES, content)):
                    dependencies.update(re.findall(Regex.STRING, possible_deps.group()))

        return dependencies, optional_dependencies
    return None, None

def search(args) -> None:
    query: str = args.query
    version: str = args.version

    session = requests.Session()

    query = did_you_mean(session, query)
    
    response = session.get(url=f"https://pypi.org/project/{query}/{f'{version}/' if version else ''}", headers=HEADERS)
    content = response.content.decode("utf-8")

    if content.find("We looked everywhere but couldn't find this page") != -1:
        print(f"No such project named {repr(query)}{f' with version {repr(version)}' if version else ''} was found.")
    else:
        html_parser = PyPIPackageHTMLParser()
        html_parser.feed(content)
        dependencies, optional_dependencies = search_dependencies(session, html_parser.package, version)
        session.close()
        string = "\n".join(
            (f"{key}: {value}" for key, value in html_parser.package.items() if key != "Links")
        )
        if "Links" in html_parser.package:
            string += f"\nLinks:\n{' '*2}" + f"\n{' '*2}".join((f"{key}: {value}" for key, value in html_parser.package["Links"]))
        if dependencies:
            string += f"\nDependencies: {dependencies}"
        if optional_dependencies:
            string += f"\nOptional Dependencies:"
            for identifier, packages in optional_dependencies:
                string += f"\n{' '*4}{repr(identifier):<10} --> {repr(packages)}"
        print(string)

def careful_install(args):
    requirement_specifier: str = args.requirement_specifier
    requirement = Requirement(requirement_specifier)
    
    session = requests.Session()

    name = did_you_mean(session, requirement.name)
    if name != requirement.name:
        requirement.name = name

    response = session.get(url=f"https://snyk.io/advisor/python/{requirement.name}/", headers=HEADERS)
    content = response.content.decode("utf-8")

    if content.find("Project Not found") != -1:
        print(f"No such project named {repr(requirement.name)} was found.")
    else:
        html_parser = SnykAdvisorHTMLParser()
        html_parser.feed(content)
        print(html_parser.package_health)

def compact_freeze(args):
    distributions = set(distribution.name for distribution in importlib.metadata.distributions())
    versions = {distribution.name: distribution.version for distribution in importlib.metadata.distributions()}
    table = {distribution.name.lower(): distribution.name for distribution in importlib.metadata.distributions()}

    for distribution in importlib.metadata.distributions():
        if distribution.requires:
            extras = set()
            for requirement_string in distribution.requires:
                requirement = Requirement(requirement_string)
                if requirement.marker is not None and requirement.marker.evaluate():
                    extra_match = re.search(r"extra == \"(.*?)\"", str(requirement.marker))
                    if extra_match:
                        extra = extra_match.group()
                        if extra not in extras:
                            extras.add(extra)
                if requirement.marker is None or requirement.marker.evaluate():
                    lowered_name = requirement.name.lower()
                    possible_names = (lowered_name,
                                      lowered_name.replace("-", "_"),
                                      lowered_name.replace("_", "-"),)
                    for name in possible_names:
                        if name in table and table[name] in distributions:
                            distributions.remove(table[name])
            if extras:
                distributions.remove(distribution.name)
                distributions.add(f"{distribution.name}[{','.join(extras)}]")
                
    distributions = sorted(distributions, key=lambda string: string.lower())
    if not args.no_version:
        distributions = (f"{distribution}=={versions[distribution]}" for distribution in distributions)
    print("\n".join(distributions))

def main() -> None:
    parser = argparse.ArgumentParser(prog="pip-ext", description="pip Additional Functionality Program")
    subparsers = parser.add_subparsers()

    parser_search = subparsers.add_parser("search")
    parser_search.add_argument("query", type=str)
    parser_search.add_argument("-v", "--version", type=str)
    parser_search.set_defaults(func=search)

    parser_careful_install = subparsers.add_parser("careful-install") # or careful-install ?
    parser_careful_install.add_argument("requirement_specifier", type=str)
    parser_careful_install.add_argument("--verbose", dest="verbose", action="store_true")
    parser_careful_install.set_defaults(func=careful_install)

    parser_compact_freeze = subparsers.add_parser("compact-freeze") # or compact-freeze ?
    parser_compact_freeze.add_argument("--no-version", dest="no_version", action="store_true")
    parser_compact_freeze.set_defaults(func=compact_freeze)

    parser_difference = subparsers.add_parser("difference")
    ...

    parser_upgrade = subparsers.add_parser("upgrade")
    ...

    parser_uninstall = subparsers.add_parser("uninstall")
    ...

    args = parser.parse_args()

    if True:
        args.func(args)
    else:
        parser.print_usage()

if __name__ == "__main__":
    main()