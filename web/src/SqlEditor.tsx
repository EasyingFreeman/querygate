import { MSSQL, PLSQL, sql } from '@codemirror/lang-sql'
import { EditorView } from '@codemirror/view'
import CodeMirror from '@uiw/react-codemirror'
import type { ReactCodeMirrorRef } from '@uiw/react-codemirror'
import { useMemo } from 'react'
import type { RefObject } from 'react'
import { objectNameParts } from './api'
import type { CatalogObject, Datasource } from './api'

const editorFeatures = { foldGutter: false, highlightActiveLineGutter: true, highlightActiveLine: true }
const labelEditor = EditorView.contentAttributes.of({ 'aria-label': 'SQL 编辑器' })

export default function SqlEditor({ editorRef, value, onChange, editable, dialect, catalog, theme }: {
  editorRef: RefObject<ReactCodeMirrorRef | null>; value: string; onChange: (value: string) => void
  editable: boolean; dialect: Datasource['dialect']; catalog: CatalogObject[]; theme: 'light' | 'dark'
}) {
  const extensions = useMemo(() => {
    const schema: Record<string, Record<string, string[]>> = Object.create(null)
    for (const item of catalog) {
      const [namespace, name] = objectNameParts(item.name)
      const namespaceTables = schema[namespace] ??= Object.create(null)
      namespaceTables[name] = item.columns.map((column) => column.name)
    }
    return [sql({ dialect: dialect === 'tsql' ? MSSQL : PLSQL, schema }), labelEditor]
  }, [catalog, dialect])

  return <CodeMirror ref={editorRef} value={value} onChange={onChange} editable={editable} extensions={extensions}
    basicSetup={editorFeatures} theme={theme} placeholder="输入只读 SQL…" height="100%" />
}
