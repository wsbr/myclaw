interface Props {
  value?: string
  onChange?: (v: string) => void
  height?: number
  language?: string
}

export default function PromptEditor({ value, onChange, height = 200, language = 'markdown' }: Props) {
  return (
    <div className="overflow-hidden rounded-md border border-border bg-background">
      <textarea
        aria-label={`${language === 'markdown' ? 'Markdown' : language} 编辑器`}
        className="block w-full resize-none bg-transparent p-3 font-mono text-[13px] leading-6 text-foreground outline-none"
        style={{ height }}
        spellCheck={false}
        value={value || ''}
        onChange={event => onChange?.(event.target.value)}
      />
    </div>
  )
}
