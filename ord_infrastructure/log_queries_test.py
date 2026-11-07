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

"""Tests for the saved Logs Insights queries: their patterns, and the log group they read."""

import json
import re
import types

import pulumi
import pulumi_aws as aws
import pytest

from ord_infrastructure import log_queries

CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def pattern(insights: str) -> re.Pattern[str]:
    """Compiles a Logs Insights regular expression, whose named groups are `(?<name>...)`."""
    return re.compile(insights.replace("(?<", "(?P<"))


def test_viewer_request_reads_an_nginx_line():
    # The `viewer` log_format in ord-app's deploy/viewer/nginx.conf.
    line = (
        "198.51.100.7 - - [07/Nov/2026:10:00:00 +0000] "
        '"GET /reactions/ord-0123?view=full HTTP/1.1" 200 512 '
        f'"https://www.google.com/" "{CHROME}"'
    )
    match = pattern(log_queries.VIEWER_REQUEST).match(line)
    assert match is not None
    assert match.groupdict() == {
        "method": "GET",
        "path": "/reactions/ord-0123",
        "status": "200",
        "referer": "https://www.google.com/",
        "agent": CHROME,
    }


def test_viewer_request_reads_a_line_with_no_referrer():
    line = '198.51.100.7 - - [07/Nov/2026:10:00:00 +0000] "POST /api/search HTTP/1.1" 200 9 "-" "-"'
    match = pattern(log_queries.VIEWER_REQUEST).match(line)
    assert match is not None
    assert (match["method"], match["path"], match["referer"]) == (
        "POST",
        "/api/search",
        "-",
    )


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/datasets",
        "/datasets/ord_dataset-0123",
        "/reactions",
        "/reactions/ord-0123",
        "/search",
        "/ask",
        "/about",
        "/browse",
        "/dataset/ord_dataset-0123",
        "/id/ord-0123",
    ],
)
def test_viewer_page_matches_the_viewer_pages(path):
    assert pattern(log_queries.VIEWER_PAGE).match(path)


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/wp-login",
        "/datasets/ord_dataset-0123/x",
        "/id",
        "/api/search",
        "/assets/x",
    ],
)
def test_viewer_page_rejects_other_paths(path):
    assert not pattern(log_queries.VIEWER_PAGE).match(path)


@pytest.mark.parametrize(
    ("agent", "crawler", "script"),
    [
        (CHROME, False, False),
        (
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            True,
            False,
        ),
        ("Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)", True, False),
        ("python-requests/2.32.3", False, True),
        ("curl/8.7.1", False, True),
    ],
)
def test_user_agent_kinds(agent, crawler, script):
    assert bool(pattern(log_queries.CRAWLER).search(agent)) == crawler
    assert bool(pattern(log_queries.SCRIPT).search(agent)) == script


def test_editor_request_reads_a_uvicorn_line():
    line = 'INFO:     10.0.97.138:0 - "POST /api/v1/auth/jit-provisioning HTTP/1.0" 201 Created'
    match = pattern(log_queries.EDITOR_REQUEST).match(line)
    assert match is not None
    assert match.groupdict() == {
        "method": "POST",
        "path": "/api/v1/auth/jit-provisioning",
        "query": "",
        "status": "201",
    }


def test_editor_request_keeps_the_query_string_apart():
    line = 'INFO:     10.0.97.138:0 - "GET /api/v1/canonicalize-smiles?smiles=C HTTP/1.0" 200 OK'
    match = pattern(log_queries.EDITOR_REQUEST).match(line)
    assert match is not None
    assert match["path"] + match["query"] == "/api/v1/canonicalize-smiles?smiles=C"
    assert match["path"] == "/api/v1/canonicalize-smiles"


def test_editor_api_calls_leave_out_the_health_check():
    query = log_queries.editor_queries("/api/v1/x?y=1")["API calls"]
    assert 'concat(path, query) != "/api/v1/x?y=1"' in query


@pytest.mark.parametrize(
    ("path", "route"),
    [
        ("/api/v1/datasets", ("datasets", None, None)),
        ("/api/v1/datasets/8913/reactions/766263", ("datasets", "reactions", None)),
        (
            "/api/v1/datasets/8913/reactions/from-scratch",
            ("datasets", "reactions", "from-scratch"),
        ),
        ("/api/v1/groups/1282/datasets/upload", ("groups", "datasets", "upload")),
        ("/api/v1/downloads/a1b2c3", ("downloads", None, None)),
        ("/api/v1/canonicalize-smiles", ("canonicalize-smiles", None, None)),
    ],
)
def test_editor_route_drops_ids(path, route):
    match = pattern(log_queries.EDITOR_ROUTE).match(path)
    assert match is not None
    assert (match["a"], match["b"], match["c"]) == route


def test_awslogs_group_reads_the_first_containers_log_group():
    definitions = json.dumps(
        [
            {
                "name": "container",
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": "service-0123abc",
                        "awslogs-region": "us-east-1",
                    },
                },
            }
        ]
    )
    assert log_queries.awslogs_group(definitions) == "service-0123abc"


@pulumi.runtime.test
def test_save_log_queries_reads_the_services_log_group(pulumi_mocks):
    definition = aws.ecs.TaskDefinition(
        "definition",
        family="service",
        container_definitions=json.dumps(
            [
                {
                    "name": "container",
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {"awslogs-group": "service-0123abc"},
                    },
                }
            ]
        ),
    )
    # A stand-in for the FargateService, which exposes its task definition as an output.
    service = types.SimpleNamespace(
        task_definition=pulumi.Output.from_input(definition)
    )
    saved = log_queries.save_log_queries(
        service,  # ty: ignore[invalid-argument-type]
        "folder",
        {"Page loads": "fields @message"},
    )

    def check(_: object) -> None:
        inputs = pulumi_mocks.inputs_of(
            "aws:cloudwatch/queryDefinition:QueryDefinition"
        )
        assert inputs["name"] == "folder/Page loads"
        assert inputs["logGroupNames"] == ["service-0123abc"]

    return pulumi.Output.all(*(query.urn for query in saved)).apply(check)  # ty: ignore[missing-argument, invalid-argument-type]
