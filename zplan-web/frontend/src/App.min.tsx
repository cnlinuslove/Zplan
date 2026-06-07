import { useState } from 'react'
import ReactDOM from 'react-dom/client'

// 根本不引入 antd / react-router / react-markdown
export default function AppMin() {
  const [count, setCount] = useState(0)
  return (
    <div style={{ padding: 40, fontFamily: 'sans-serif', background: '#111', color: '#0f0', minHeight: '100vh' }}>
      <h1>✅ Z-Plan React {ReactDOM.version}</h1>
      <p>Pure React only — no antd, no router, no markdown</p>
      <button
        onClick={() => setCount(c => c + 1)}
        style={{ padding: '8px 24px', fontSize: 18, marginTop: 16, cursor: 'pointer' }}
      >
        Count: {count}
      </button>
    </div>
  )
}
