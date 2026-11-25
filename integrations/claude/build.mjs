import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const source = fileURLToPath(new URL('.', import.meta.url))
const workspace = resolve(source, '../..')
const output = join(workspace, 'dist', 'claude')
const python = process.env.QUERYGATE_BUILD_PYTHON || 'python'
const { version } = JSON.parse(readFileSync(join(source, 'package.json'), 'utf8'))
const repository = 'https://github.com/EasyingFreeman/QueryGate'
const description = 'Query databases and submit authorized, audited mutations through QueryGate.'
const entry = 'server/querygate.cjs'
const settings = {
  gateway_url: {
    type: 'string', title: 'QueryGate URL', required: true,
    description: 'Base URL of your QueryGate REST service. Use HTTPS when it runs on another computer.',
    default: 'http://127.0.0.1:8000',
  },
  gateway_token: {
    type: 'string', title: 'QueryGate client token', required: true, sensitive: true,
    description: 'The scoped client token configured on your QueryGate service.',
  },
}

function launch(root) {
  return {
    command: 'node',
    args: [`${root}/${entry}`],
    env: {
      QUERYGATE_URL: '${user_config.gateway_url}',
      QUERYGATE_MCP_TOKEN: '${user_config.gateway_token}',
    },
  }
}

function writeJson(directory, path, data) {
  writeFileSync(join(directory, path), `${JSON.stringify(data, null, 2)}\n`)
}

function licenses(inputs) {
  const directories = new Set()
  for (const file of Object.keys(inputs)) {
    const path = file.replaceAll('\\', '/')
    const dependency = [...path.matchAll(/(?:^|\/)node_modules\/(@[^/]+\/[^/]+|[^/]+)/g)].at(-1)
    if (dependency) directories.add(resolve(source, path.slice(0, dependency.index + dependency[0].length)))
  }
  return [...directories].sort().map((directory) => {
    const { name, version } = JSON.parse(readFileSync(join(directory, 'package.json'), 'utf8'))
    const notices = readdirSync(directory).filter((file) => /^(license|licence|copying|notice)([._-]|$)/i.test(file))
    if (notices.length === 0) throw new Error(`Missing license text for bundled dependency ${name}`)
    return [`${name} ${version}`, ...notices.sort().map((file) => readFileSync(join(directory, file), 'utf8'))].join('\n\n')
  }).join('\n\n' + '-'.repeat(75) + '\n\n')
}

const { tools, bearerPattern } = JSON.parse(execFileSync(python, ['-c', `
import json
from querygate.config import BEARER_TOKEN
from querygate.mcp_server import tool_definitions
print(json.dumps({
    'tools': [tool.model_dump(by_alias=True, exclude_none=True) for tool in tool_definitions()],
    'bearerPattern': BEARER_TOKEN.pattern,
}))
`], { cwd: workspace, encoding: 'utf8' }))

const bundle = await build({
  absWorkingDir: source, entryPoints: ['bridge.mjs'], bundle: true, format: 'cjs',
  platform: 'node', target: 'node20', minify: true, legalComments: 'eof', metafile: true, write: false,
  define: {
    QUERYGATE_TOOLS: JSON.stringify(tools),
    QUERYGATE_VERSION: JSON.stringify(version),
    QUERYGATE_BEARER_PATTERN: JSON.stringify(bearerPattern),
  },
})
const notices = licenses(bundle.metafile.inputs)
const instructions = readFileSync(join(source, 'INSTALL.md'))

function writeBridge(directory) {
  mkdirSync(join(directory, 'server'), { recursive: true })
  writeFileSync(join(directory, entry), bundle.outputFiles[0].contents)
  writeFileSync(join(directory, 'THIRD-PARTY-LICENSES.txt'), notices)
}

function archive(directory, target, root = '') {
  execFileSync(python, ['-c', `
from pathlib import Path
from sys import argv
from zipfile import ZIP_DEFLATED, ZipFile
source, target, root = Path(argv[1]), Path(argv[2]), argv[3]
with ZipFile(target, 'w', compression=ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(source.rglob('*')):
        if path.is_file():
            archive.write(path, (Path(root) / path.relative_to(source)).as_posix())
`, directory, target, root])
}

mkdirSync(output, { recursive: true })
const staging = mkdtempSync(join(output, 'package-'))
try {
  const desktop = join(staging, 'desktop')
  const marketplace = join(staging, 'querygate-claude-code')
  const plugin = join(marketplace, 'plugins', 'querygate')
  mkdirSync(join(marketplace, '.claude-plugin'), { recursive: true })
  mkdirSync(join(plugin, '.claude-plugin'), { recursive: true })
  writeBridge(desktop)
  writeBridge(plugin)

  writeJson(desktop, 'manifest.json', {
    manifest_version: '0.3', name: 'querygate', display_name: 'QueryGate', version, description,
    author: { name: 'QueryGate', url: repository }, homepage: repository,
    server: { type: 'node', entry_point: entry, mcp_config: launch('${__dirname}') },
    compatibility: { runtimes: { node: '>=20.3.0' } },
    tools: tools.map(({ name, description }) => ({ name, description })),
    user_config: settings,
  })
  writeJson(plugin, '.claude-plugin/plugin.json', {
    name: 'querygate', displayName: 'QueryGate', version, description, author: { name: 'QueryGate' }, homepage: repository,
    userConfig: settings,
  })
  writeJson(plugin, '.mcp.json', { mcpServers: { querygate: { type: 'stdio', ...launch('${CLAUDE_PLUGIN_ROOT}') } } })
  writeJson(marketplace, '.claude-plugin/marketplace.json', {
    name: 'querygate-local', description: 'Local installation of the QueryGate Claude plugin.', owner: { name: 'QueryGate' },
    plugins: [{ name: 'querygate', source: './plugins/querygate', description, version }],
  })
  writeFileSync(join(marketplace, 'INSTALL.md'), instructions)
  writeFileSync(join(desktop, 'INSTALL.md'), instructions)

  const desktopName = `QueryGate-Claude-Desktop-${version}.mcpb`
  const codeName = `QueryGate-Claude-Code-${version}.zip`
  archive(desktop, join(output, desktopName))
  archive(marketplace, join(output, codeName), 'querygate-claude-code')
  const checksum = [desktopName, codeName].map((name) => (
    `${createHash('sha256').update(readFileSync(join(output, name))).digest('hex')}  ${name}`
  ))
  writeFileSync(join(output, 'SHA256SUMS.txt'), `${checksum.join('\n')}\n`)
  writeFileSync(join(output, 'INSTALL.md'), instructions)
  console.log(`Claude packages written to ${output}`)
} finally {
  const child = relative(output, staging)
  if (child.startsWith('package-') && !child.includes(sep)) rmSync(staging, { recursive: true, force: true })
}
