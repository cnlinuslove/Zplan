// 测试：React + antd (Card)
import { StrictMode, version } from 'react'
import { createRoot } from 'react-dom/client'
import { ConfigProvider, Card } from 'antd'
import zhCN from 'antd/locale/zh_CN'

function App() {
  return (
    <ConfigProvider locale={zhCN}>
      <div style={{ background: '#1a1a2e', padding: 40, minHeight: '100vh', color: '#fff' }}>
        <h1>✅ React {version} + antd</h1>
        <Card title="测试" style={{ maxWidth: 400 }}>
          <p>如果看到这个 Card，antd 正常。</p>
        </Card>
      </div>
    </ConfigProvider>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode><App /></StrictMode>
)
