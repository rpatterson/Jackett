"""
A Home Assistant ``command_line`` sensor for monitoring an indexer user account.

Returns the user's site-wide ratio, notifications, messages, system messages, H&Rs,
etc. as the state for a Home Assistant sensor entity. Useful to set up alerts for when
the user's attention is required, to graph ratio progress, etc..

Write the API key for the Torznab server in the `~/.netrc` for the user running Home
Assistant. Add the corresponding per-indexer configuration to
`/config/packages/indexers.yaml` and restart Home Assistant.
"""

import sys
import pathlib
import urllib.parse
import json
import netrc
import argparse
import logging

# Dependencies baked into the Home Assistant image:
import requests  # type: ignore
import xmltodict  # type: ignore
import jinja2
import yaml  # type: ignore

TORZNAB_TYPES = {
    "Jackett": {
        "configured_key": "configured",
        "torznab_path": "/api/v2.0/indexers/{indexer[id]}/results/torznab/",
    },
    "Prowlarr": {
        "torznab_path": "/{indexer[id]}/api",
        "slug_key": "definitionName",
    },
}
# Most `<torznab:attr ...` values are integers, only map the exceptions:
TORZNAB_ATTR_TYPES = {"minimumratio": float}

logger = logging.getLogger(__name__)


# https://stackoverflow.com/a/51026159/624787
class BaseURLSession(requests.Session):
    """
    Send all requests to sub-paths of the given base URL.
    """

    def __init__(self, base_url=None):
        """
        Capture a reference to the base URL.
        """
        super().__init__()
        self.base_url = base_url

    def request(self, method, url, *args, **kwargs):
        """
        Prepend the base URL to the path.
        """
        joined_url = urllib.parse.urljoin(self.base_url, url)
        return super().request(method, joined_url, *args, **kwargs)


def query_indexer(indexer_id: str, session: requests.Session) -> dict:
    """
    Query the user's site history from a specific indexer.
    """
    # TODO: Not actually necessary ATM because so far the site-wide user data Torznab
    # category is fixed and shared across indexers. The following support for looking it
    # up dynamically is a Proof Of Concept (POC) for other use cases that do have to
    # lookup the category IDs that may vary between indexer implementation types, such
    # as those querying user-specific torrent history:
    session, caps_path = maybe_download_caps(indexer_id, session)
    caps = xmltodict.parse(caps_path.read_text())
    category = next(
        category
        for category in caps["caps"]["categories"]["category"]
        if category["@name"] == "User/Site"
    )

    # The Torznab site-wide user data category 109100 "User/Site" row:
    site_user_response = session.get(
        "",
        params={"t": "search", "cat": category["@id"]},
    )
    site_user_data = xmltodict.parse(site_user_response.text)["rss"]["channel"]["item"]
    if isinstance(site_user_data, list):
        site_user_data = site_user_data[0]
    # Do any type conversions we can rely on from the Torznab RSS:
    site_user_data["category"] = [
        int(category) for category in site_user_data["category"]
    ]
    site_user_data["enclosure"]["@length"] = int(site_user_data["enclosure"]["@length"])
    site_user_data["size"] = int(site_user_data["size"])

    # Un-map the mapped Torznab fields:
    script_path = pathlib.Path(sys.argv[0])
    with open(
        script_path.parents[4] / "src" / "Jackett.Common" / "Definitions" /
        ".torznab-mapped-fields.yaml",
        encoding="utf-8",
    ) as torznab_mapped_opened:
        torznab_mapped_fields = yaml.load(torznab_mapped_opened, Loader=yaml.CLoader)
    for torznab_attr in site_user_data["torznab:attr"]:
        site_user_data.setdefault(
            torznab_attr["@name"],
            TORZNAB_ATTR_TYPES.get(torznab_attr["@name"], int)(torznab_attr["@value"])
        )
    del site_user_data["torznab:attr"]
    for torznab_field, mapped_field in torznab_mapped_fields["siteUser"].items():
        if torznab_field in site_user_data:
            site_user_data[mapped_field] = site_user_data.pop(torznab_field)
    site_user_data.update(
        json.loads(site_user_data.pop("description")),
    )

    return site_user_data


def get_indexers(
    session: requests.Session,
) -> tuple:
    """
    Retrieve the indexers from the Torznab server.
    """
    torznab_type = None
    # Try Jackett first because it caches query results and sends fewer requests to the
    # indexer:
    dashboard_response = session.get("/UI/Dashboard")
    try:
        dashboard_response.raise_for_status()
    except requests.HTTPError:
        # Next try Prowlarr:
        indexers_response = session.get("/api/v1/indexer")
        try:
            indexers_response.raise_for_status()
        except requests.HTTPError as exc:
            raise NotImplementedError("Could not identify Torznab server type") from exc
        torznab_type = TORZNAB_TYPES["Prowlarr"]
    else:
        torznab_type = TORZNAB_TYPES["Jackett"]
        indexers_response = session.get("/api/v2.0/indexers")
    return torznab_type, indexers_response.json()


def maybe_download_caps(
    indexer_id: str,
    session: requests.Session,
) -> tuple:
    """
    Download the Torznab capabilities if this or the indexers config is more recent.
    """
    script_path = pathlib.Path(sys.argv[0])
    config_template = script_path.parent / "indexers.yaml.in"
    caps_path = script_path.parent / f"{indexer_id}.caps.rss"

    if caps_path.exists():
        prereq_mtime_min = min(
            script_path.stat().st_mtime,
            config_template.stat().st_mtime,
        )
        if caps_path.stat().st_mtime > prereq_mtime_min:
            caps = xmltodict.parse(caps_path.read_text())
            torznab_type = TORZNAB_TYPES[caps["caps"]["server"]["@title"]]
            indexer = {"id": indexer_id}
            session.base_url = (
                f"{session.base_url}{torznab_type['torznab_path'].format(indexer=indexer)}"
            )
            return session, caps_path

    torznab_type, indexers = get_indexers(session)
    indexer = next(
        indexer_data
        for indexer_data in indexers
        if (
            (
                "configured_key" not in torznab_type
                or indexer_data[torznab_type["configured_key"]],
            )
            and indexer_data["id"] == indexer_id
        )
    )
    session.base_url = (
        f"{session.base_url}{torznab_type['torznab_path'].format(indexer=indexer)}"
    )

    caps_path.write_text(session.get("", params={"t": "caps"}).text)
    return session, caps_path


# Define command line options and arguments
parser = argparse.ArgumentParser(
    description=__doc__,
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.add_argument(
    "--torznab-server", "-s",
    default="http://localhost:9117",
    help="The API URL for the Torznab server, such as from Jackett/Prowlarr",
)
subparsers = parser.add_subparsers(
    dest="command",
    required=True,
    help="subcommand",
)


def query(
    indexer_id: str,
    session: requests.Session,
    torznab_server: str = parser.get_default(  # pylint: disable=unused-argument
        "--torznab-server",
    ),
):
    """
    Query the Torznab server for the user's site history and return the JSON.
    """
    json.dump(query_indexer(indexer_id, session), sys.stdout, indent=2)


query.__doc__ = query.__doc__
parser_query = subparsers.add_parser(
    "query",
    help=str(query.__doc__).strip(),
    description=str(query.__doc__).strip(),
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser_query.add_argument(
    "indexer_id",
    help="The ID of the indexer, a slug in Jackett, a DB integer ID in Prowlarr",
)
parser_query.set_defaults(command=query)


def render(
    indexer_tag: str,
    session: requests.Session,
    torznab_server: str = parser.get_default(  # pylint: disable=unused-argument
        "--torznab-server",
    ),
):
    """
    Render the Home Assistant sensor and alert configurations for the tagged indexers.
    """
    torznab_type, all_indexers = get_indexers(session)
    indexers = []
    for indexer in all_indexers:
        if (
            (
                "configured_key" in torznab_type
                and not indexer[torznab_type["configured_key"]]
            )
            or indexer_tag not in indexer["tags"]
        ):
            continue
        indexer["slug"] = indexer[torznab_type.get("slug_key", "id")].replace("-", "_")
        indexer["site_user"] = query_indexer(indexer["id"], session)
        indexers.append(indexer)

    script_path = pathlib.Path(sys.argv[0])
    jinja_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(script_path.parent),
        autoescape=jinja2.select_autoescape()
    )
    jinja_template = jinja_env.get_template(f"{script_path.stem}.yaml.in")
    print(jinja_template.render(indexers=indexers))


render.__doc__ = render.__doc__
parser_render = subparsers.add_parser(
    "render",
    help=str(render.__doc__).strip(),
    description=str(render.__doc__).strip(),
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser_render.add_argument(
    "--indexer-tag", "-t",
    default="home-assistant",
    help="The Torznab server tag for the indexers to configure sensors for",
)
parser_render.set_defaults(command=render)


def main(args=None):  # pylint: disable=missing-function-docstring
    parsed_args = parser.parse_args(args=args)
    cli_kwargs = dict(vars(parsed_args))
    # Remove any meta options and arguments, those used to direct option and argument
    # handling:
    del cli_kwargs["command"]

    session = BaseURLSession(cli_kwargs["torznab_server"])
    authenticators = netrc.netrc().authenticators(
        urllib.parse.urlsplit(cli_kwargs["torznab_server"]).netloc,
    )
    if authenticators is None or not authenticators[2]:
        raise ValueError(
            "Could not lookup authentication from ~/.netrc: "
            f"{cli_kwargs['torznab_server']}",
        )
    session.params["apikey"] = authenticators[2]

    parsed_args.command(session=session, **cli_kwargs)


main.__doc__ = __doc__


if __name__ == "__main__":
    main()
