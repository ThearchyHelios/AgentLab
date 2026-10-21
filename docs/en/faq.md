English | [Simplified Chinese](../zh-CN/faq.md)

# FAQ

This page lists common errors and symptoms, and what to do about them. It is for everyone. The user interface is currently available in Simplified Chinese only.

## The model returns empty content

Models after Claude 4.6 enable thinking by default, and with too small a `max_tokens` you get "Thinking used up the budget; the body is empty". Increase the node's max_tokens, or turn thinking mode off. The timeline shows a matching hint.

## A relay gateway returns 401

Some gateways accept only `Authorization: Bearer` and reject requests that also carry `x-api-key`. In the provider settings, tick Authenticate with Authorization: Bearer.

## Data source schema introspection fails

A failed introspection and "Not yet introspected" are two different states. Clicking the button won't fix a failure; deal with the timeout or change the schema first. The data source card shows the failure reason and the databases available on that server. Agents are told the facts too, including that they can query `information_schema` directly to confirm the table structure.

## The web tool says an address resolved to 198.18.x.x and was blocked

The local proxy (Clash, mihomo, and so on) has TUN + fake-ip turned on, so looking up any domain first returns a fake address in the 198.18 range, which is then blocked as an internal address.

- Add the line `AGENTLAB_HTTP_TOOL_FAKE_IP_RANGES=198.18.0.0/15` to `.env` and restart the backend. Resolution results in that range are then verified against public DNS to find the real address: public addresses are allowed, internal ones are still blocked.
- The DoH URLs used for verification are set by `AGENTLAB_HTTP_TOOL_DOH_URLS` (comma-separated).
- If you'd rather not change the configuration, switch the proxy's DNS to redir-host, or add the domains you need to reach to `fake-ip-filter`.

## Port conflicts

The frontend defaults to 5273 and the backend to 8000; change them with `AGENTLAB_WEB_PORT` / `AGENTLAB_PORT`.

## After changing backend code, a running run shows as interrupted

`dev.sh` has hot reload on, so changing a backend file means a service restart. The run is suspended (not cancelled), and its checkpoint is intact: click Resume on the run details page. Finished nodes don't run again, and later human approvals still stop and wait for a person.

## A run is aborted for exceeding the time limit

A single execution (the stretch between two human interventions) is limited to 600 seconds by default, and a single model call times out after 300 seconds without output by default. Most likely the model or a tool isn't responding. If it really needs longer, increase `AGENTLAB_MAX_RUN_SECONDS` / `AGENTLAB_MODEL_TIMEOUT_SECONDS`. The current values are shown under Settings → Runtime Environment.

## A run reports that it used up N steps without finishing

A loop gets its own step budget from the iteration count declared in its `max_iterations`, so it can run as many iterations as configured, unconstrained by the global limit. Hitting this error usually means a cycle where a branch connects back upstream with no loop node to cap its iterations: wrap it in a loop node. If you really need more steps, increase `AGENTLAB_MAX_GRAPH_STEPS` (default 200).

## An agent wrapped up before it finished querying

A hint in the timeline states the reason: the steps were used up, several consecutive steps brought no new information, the token or spending budget ran out, or the context approached the window limit. Following the hint, adjust the steps or budget on the node or in Settings (budgets can be set to unlimited); see [Guardrails](workflows.md#guardrails).
