import { McpServer, fromJsonSchema } from '@modelcontextprotocol/server'
import { serveStdio } from '@modelcontextprotocol/server/stdio'
import { AjvJsonSchemaValidator } from '@modelcontextprotocol/server/validators/ajv'

const token = process.env.QUERYGATE_MCP_TOKEN ?? ''
const tokenPattern = new RegExp(`^(?:${QUERYGATE_BEARER_PATTERN})$`)
if (token.match(tokenPattern)?.[0] !== token) {
  console.error('QueryGate needs a valid client token. Configure it in the Claude plugin settings.')
  process.exit(1)
}

function gatewayUrl() {
  const value = process.env.QUERYGATE_URL || 'http://127.0.0.1:8000'
  try {
    const url = new URL(value)
    const loopback = ['127.0.0.1', '[::1]', 'localhost'].includes(url.hostname)
    if ((url.protocol === 'https:' || (url.protocol === 'http:' && loopback))
      && !url.username && !url.password && !url.search && !url.hash) {
      if (!url.pathname.endsWith('/')) url.pathname += '/'
      return url
    }
  } catch {
    // The configured value can contain a credential. Keep it out of the message.
  }
  console.error('QueryGate needs an HTTPS service URL or an HTTP loopback URL, without credentials, query or fragment.')
  process.exit(1)
}

const base = gatewayUrl()
const validator = new AjvJsonSchemaValidator()
const gatewayResponseError = () => failure('GATEWAY_RESPONSE_ERROR', 'The QueryGate API returned an unexpected response.')

function result(data, isError = false) {
  return { content: [{ type: 'text', text: JSON.stringify(data) }], structuredContent: data, isError }
}

function failure(code, message) {
  return result({ code, message }, true)
}

async function callGateway(name, args, signal) {
  const execute = name === 'execute_query'
  const mutation = name === 'submit_mutation'
  const post = execute || mutation
  const bound = mutation ? [...Object.values(args.key), ...Object.values(args.values)] : Object.values(args.params ?? {})
  if (post && bound.some((value) => Number.isInteger(value) && !Number.isSafeInteger(value))) {
    return failure('INVALID_PARAMS', 'Pass integer parameters outside the JavaScript safe range as strings.')
  }

  let url
  if (name === 'get_schema') {
    url = new URL('v1/schema', base)
    for (const [key, value] of Object.entries({ limit: 50, include_columns: false, ...args })) {
      if (value !== undefined && value !== null) url.searchParams.set(key, String(value))
    }
  } else if (mutation) url = new URL('v1/mutations', base)
  else if (name === 'get_mutation') url = new URL(`v1/mutations/${encodeURIComponent(args.operation_id)}`, base)
  else url = new URL(execute ? 'v1/query' : 'v1/datasources', base)

  const deadline = AbortSignal.timeout(65_000)
  let response
  let data
  try {
    response = await fetch(url, {
      method: post ? 'POST' : 'GET',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/json',
        ...(post ? { 'Content-Type': 'application/json' } : {}),
      },
      body: post ? JSON.stringify(args) : undefined,
      redirect: 'manual',
      signal: AbortSignal.any([signal, deadline]),
    })
    data = await response.json()
  } catch (error) {
    if (deadline.aborted) return failure('GATEWAY_TIMEOUT', 'The QueryGate API did not respond in time.')
    if (signal.aborted) throw error
    return response ? gatewayResponseError() : failure('GATEWAY_UNAVAILABLE', 'The QueryGate API could not be reached.')
  }

  if (response.ok) return result(name === 'list_datasources' ? { datasources: data } : data)
  if (response.status >= 400 && typeof data?.code === 'string' && typeof data?.message === 'string') {
    const details = { code: data.code, message: data.message }
    if (typeof data.query_id === 'string') details.query_id = data.query_id
    if (typeof data.diagnostic === 'string') details.diagnostic = data.diagnostic
    return result(details, true)
  }
  return gatewayResponseError()
}

serveStdio(() => {
  const server = new McpServer({ name: 'querygate', version: QUERYGATE_VERSION }, {
    instructions: 'List authorized datasources and browse objects with get_schema. Supply the user’s actual business reason for each query or mutation. Use submit_mutation for enabled INSERT or UPDATE. A pending mutation has not executed. Report its operation ID and wait for approval.',
  })
  for (const tool of QUERYGATE_TOOLS) {
    server.registerTool(tool.name, {
      description: tool.description,
      annotations: tool.annotations,
      inputSchema: fromJsonSchema(tool.inputSchema, validator),
      outputSchema: fromJsonSchema(tool.outputSchema, validator),
    }, (args, context) => callGateway(tool.name, args, context.mcpReq.signal))
  }
  return server
})
