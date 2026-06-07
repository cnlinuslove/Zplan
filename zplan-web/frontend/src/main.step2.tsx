// 测试：React + antd + react-router-dom
import { StrictMode, version } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ConfigProvider, Card } from 'antd'
import zhCN from 'antd/locale/zh_CN'

function App() {
  return (
    <ConfigProvider locale={zhCN}>
      <BrowserRouter>
        <div style={{ background: '#1a1a2e', padding: 40, minHeight: '100vh', color: '#fff' }}>
          <h1>✅ React {version} + antd + react-router</h1>
          <Card title="测试 2" style={{ maxWidth: 400 }}>
            <p>如果看到这个 Card，antd + react-router 都正常。</p>
          </Card>
        </div>
      </BrowserRouter>
    </ConfigProvider>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode><App /></StrictMode>
)
