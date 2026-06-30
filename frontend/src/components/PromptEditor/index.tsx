import MonacoEditor from '@monaco-editor/react'

interface Props {
  value?: string
  onChange?: (v: string) => void
  height?: number
  language?: string
}

export default function PromptEditor({ value, onChange, height = 200, language = 'markdown' }: Props) {
  return (
    <div style={{ border: '1px solid #d9d9d9', borderRadius: 6, overflow: 'hidden' }}>
      <MonacoEditor
        height={height}
        language={language}
        value={value}
        onChange={(v) => onChange?.(v || '')}
        options={{
          minimap: { enabled: false },
          lineNumbers: 'off',
          wordWrap: 'on',
          scrollBeyondLastLine: false,
          fontSize: 13,
          padding: { top: 8, bottom: 8 },
        }}
        theme="vs"
      />
    </div>
  )
}
