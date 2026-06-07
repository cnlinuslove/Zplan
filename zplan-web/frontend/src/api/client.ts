import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 120_000,
})

export default api

// ── SSE streaming helper ──
export async function* sseStream(
  url: string,
  body: Record<string, unknown>,
): AsyncGenerator<Record<string, unknown>> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  if (!response.ok) throw new Error(`HTTP ${response.status}`)

  const reader = response.body?.getReader()
  if (!reader) throw new Error('No response body')

  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const lines = buffer.split('\n\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      for (const event of line.split('\n')) {
        if (event.startsWith('data: ')) {
          try {
            yield JSON.parse(event.slice(6))
          } catch {
            // skip unparseable
          }
        }
      }
    }
  }
}
