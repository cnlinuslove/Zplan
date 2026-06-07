// 绝对最小测试 — 只 import React，不用任何第三方库
import { useState, StrictMode, version } from 'react'
import { createRoot } from 'react-dom/client'

function App() {
  const [count, setCount] = useState(0)
  return (
    <div style={{
      background: '#1a1a2e', color: '#0f0', padding: 40,
      fontFamily: 'monospace', minHeight: '100vh',
    }}>
      <h1>✅ React {version} OK</h1>
      <p>useState works. No antd, no router, no react-markdown.</p>
      <button
        onClick={() => setCount(c => c + 1)}
        style={{ padding: '10px 30px', fontSize: 18, cursor: 'pointer', marginTop: 16 }}
      >
        Count: {count}
      </button>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode><App /></StrictMode>
)
