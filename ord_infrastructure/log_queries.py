# Copyright 2026 Open Reaction Database Project Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Saved Logs Insights queries that count the sites' visits from their container logs.

Each query reads the lines a service's container writes to CloudWatch and reports totals:
the viewer's from nginx's access log, in the `viewer` log_format of ord-app's
deploy/viewer/nginx.conf, and the editor's from uvicorn's access log and the backend's
own log. The regular expressions below use Logs Insights' syntax, with `(?<name>...)`
for a named group.
"""

import json
from collections.abc import Mapping

import pulumi
import pulumi_aws as aws
import pulumi_awsx as awsx

# A request line in the viewer's nginx log: client, user, time, request, status, bytes,
# referrer, and user agent.
VIEWER_REQUEST = (
    r'^\S+ - \S+ \[[^\]]+\] "(?<method>[A-Z]+) (?<path>[^ ?]*)\S* [^"]*" '
    r'(?<status>\d{3}) \d+ "(?<referer>[^"]*)" "(?<agent>[^"]*)"'
)

# The viewer's pages, including the ord-interface paths it redirects; the single-page
# app answers any other path too, so scanners' guesses would otherwise count as pages.
VIEWER_PAGE = r"^\/((datasets|reactions)(\/[^\/]+)?|search|ask|about|browse|(dataset|id)\/[^\/]+)?\/?$"

# User agents that name a crawler, a link preview, or a script rather than a browser.
CRAWLER = r"(?i)bot|crawl|spider|slurp|preview|scan|headless"
SCRIPT = (
    r"(?i)python|curl|wget|go-http|java|okhttp|libwww|httpx|requests|axios|node|httr"
)

# A request line in uvicorn's access log, behind the editor's nginx.
EDITOR_REQUEST = (
    r'^INFO:\s+\S+ - "(?<method>[A-Z]+) (?<path>[^ ?]*)(?<query>\S*) HTTP\/[0-9.]+" '
    r"(?<status>\d{3})"
)

# The route an editor API path follows, without its IDs: the segments with no digit.
EDITOR_ROUTE = (
    r"^\/api\/v1\/(?<a>[a-z_-]+)(?:\/[^\/]*\d[^\/]*)?(?:\/(?<b>[a-z_-]+))?"
    r"(?:\/[^\/]*\d[^\/]*)?(?:\/(?<c>[a-z_-]+))?"
)

# Full loads of a viewer page by a browser, which happen on arriving at the site or
# reloading a page; moving between pages inside the app loads none.
_VIEWER_PAGE_LOADS = f"""parse @message /{VIEWER_REQUEST}/
| filter method = "GET" and status in ["200", "304"]
| filter path like /{VIEWER_PAGE}/
| filter agent not like /{CRAWLER}/ and agent not like /{SCRIPT}/"""

VIEWER_QUERIES = {
    "Visits per day": f"""{_VIEWER_PAGE_LOADS}
| stats count() as page_loads by bin(1d) as day
| sort day desc""",
    "Landing pages": f"""{_VIEWER_PAGE_LOADS}
| parse path /^\\/(?<section>[a-z]+)(?<item>\\/[^\\/]+)?/
| stats count() as page_loads
    by concat("/", coalesce(section, ""), if(ispresent(item), "/…", "")) as page
| sort page_loads desc""",
    "Traffic sources": f"""{_VIEWER_PAGE_LOADS}
| parse referer /^https?:\\/\\/(?<site>[^\\/:]+)/
| stats count() as page_loads by coalesce(site, "(none)") as source
| sort page_loads desc""",
    "API calls": f"""parse @message /{VIEWER_REQUEST}/
| parse path /^\\/api\\/(?<resource>datasets|reactions|search|ask|download)(?<rest>[^ ]*)$/
| filter ispresent(resource) and status like /^2/
| fields if(agent like /{CRAWLER}/, "crawler",
    if(agent like /{SCRIPT}/, "script", "browser")) as client,
  if(rest = "", "", if(rest = "/newest", " newest",
    if(rest like /\\/compounds$/, " compounds", " one"))) as part
| stats count() as calls by client, concat(method, " ", resource, part) as call
| sort calls desc""",
}


def editor_queries(health_check_path: str) -> dict[str, str]:
    """Returns the editor's queries, which leave out the load balancer's health checks.

    Args:
        health_check_path: The path, with its query string, that the load balancer's
            health checks request.

    Returns:
        Each query, by its title.
    """
    health_check = json.dumps(health_check_path)
    return {
        "Visits and new accounts per day": """filter @message like /"POST \\/api\\/v1\\/auth\\/jit-provisioning HTTP\\/[0-9.]+" 2\\d\\d/
  or @message like /New <User\\(id=/
| stats sum(@message like /jit-provisioning/) as visits,
    sum(@message like /New <User/) as new_accounts
    by bin(1d) as day
| sort day desc""",
        "API calls": f"""parse @message /{EDITOR_REQUEST}/
| filter ispresent(method) and concat(path, query) != {health_check}
| parse path /{EDITOR_ROUTE}/
| stats count() as calls
    by method,
      concat(a, if(ispresent(b), concat("/", b), ""), if(ispresent(c), concat("/", c), ""))
        as route,
      status like /^2/ as succeeded
| sort calls desc""",
    }


def awslogs_group(container_definitions: str) -> str:
    """Returns the log group the first container in a task definition writes to.

    Args:
        container_definitions: A task definition's container definitions, as JSON.

    Returns:
        The container's `awslogs-group` option.
    """
    container = json.loads(container_definitions)[0]
    return container["logConfiguration"]["options"]["awslogs-group"]


def _log_group_names(
    definition: aws.ecs.TaskDefinition | None,
) -> pulumi.Output[list[str]]:
    """Returns the log group a task definition's container writes to, as a list."""
    if definition is None:
        return pulumi.Output.from_input([])
    return definition.container_definitions.apply(lambda text: [awslogs_group(text)])  # ty: ignore[missing-argument, invalid-argument-type]


def save_log_queries(
    service: awsx.ecs.FargateService, folder: str, queries: Mapping[str, str]
) -> list[aws.cloudwatch.QueryDefinition]:
    """Saves Logs Insights queries over the log group a service's container writes to.

    Args:
        service: The service whose container log the queries read.
        folder: The folder the queries are saved in, such as the service's cluster name.
        queries: Each query, by its title.

    Returns:
        The saved queries, in the order given.
    """
    log_group_names = service.task_definition.apply(_log_group_names)  # ty: ignore[missing-argument, invalid-argument-type]
    return [
        aws.cloudwatch.QueryDefinition(
            f"{folder}_{title.lower().replace(' ', '_')}_query",
            name=f"{folder}/{title}",
            query_string=query,
            log_group_names=log_group_names,  # ty: ignore[invalid-argument-type]
        )
        for title, query in queries.items()
    ]
