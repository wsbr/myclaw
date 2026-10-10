export function parseEvalDefinition(text: string) {
  const raw = JSON.parse(text)
  const value = raw?.definition ?? raw
  if (!value || typeof value.name !== 'string' || !value.name.trim()
    || !Array.isArray(value.cases) || !value.cases.length || value.cases.length > 20) {
    throw new Error('文件需要评测集名称和 1–20 个用例')
  }
  const keys = new Set<string>()
  for (const item of value.cases) {
    if (!item || typeof item.case_key !== 'string' || !item.case_key.trim()
      || typeof item.name !== 'string' || !item.name.trim()
      || typeof item.input_text !== 'string' || !item.input_text.trim()
      || !item.assertions || typeof item.assertions !== 'object' || Array.isArray(item.assertions)) {
      throw new Error('每个用例需要唯一标识、名称、输入和断言')
    }
    const key = item.case_key.trim()
    if (keys.has(key)) throw new Error(`重复用例标识：${key}`)
    keys.add(key)
  }
  return value
}

export function downloadEvalJson(name: string, value: unknown) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${name.replace(/[\\/:*?"<>|]/g, '_')}.json`
  anchor.click()
  URL.revokeObjectURL(url)
}
