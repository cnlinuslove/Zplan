import { useState, useRef, useEffect } from 'react'
import { Input, Button, Space, Card, Tag, Image } from 'antd'
import { useNavigate } from 'react-router-dom'
import { SendOutlined, PlusOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import { useChatStore } from '../stores/chatStore'
import { sseStream } from '../api/client'

export default function ChatPage() {
  const navigate = useNavigate()
  const { sessionId, messages, loading, setSessionId, addMessage, appendToken, setLoading, clearMessages } =
    useChatStore()
  const [input, setInput] = useState('')
  const listRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<any>(null)

  // 自动滚到底部
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  // 聚焦输入框
  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  async function send() {
    const text = input.trim()
    if (!text || loading) return
    setInput('')
    setLoading(true)
    addMessage({ role: 'user', content: text })

    // 添加一个空的 assistant 消息用于流式填充
    const streamMsg: any = { role: 'assistant', content: '', streaming: true }
    useChatStore.setState((s) => ({ messages: [...s.messages, streamMsg] }))

    try {
      const stream = sseStream('/api/v1/chat/send', {
        text,
        session_id: sessionId,
        stream: true,
      })

      for await (const event of stream) {
        if (event.type === 'token') {
          appendToken(event.text as string)
        } else if (event.type === 'done') {
          useChatStore.setState((s) => {
            const msgs = [...s.messages]
            const last = msgs[msgs.length - 1]
            if (last && last.streaming) {
              msgs[msgs.length - 1] = {
                ...last,
                streaming: false,
                intent: event.intent as string,
                chart: event.chart as any,
              }
            }
            return { messages: msgs }
          })
          if (event.session_id) setSessionId(String(event.session_id))
        } else if (event.type === 'error') {
          console.error('Chat error:', event.message)
        }
      }
    } catch (err) {
      console.error('Stream failed:', err)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      {/* 标题栏 */}
      <div
        style={{
          padding: '12px 24px',
          borderBottom: '1px solid #303030',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <span style={{ fontSize: 16, fontWeight: 600 }}>Z-Plan 对话</span>
        <Button size="small" icon={<PlusOutlined />} onClick={clearMessages}>
          新对话
        </Button>
      </div>

      {/* 消息列表 */}
      <div
        ref={listRef}
        style={{
          flex: 1,
          overflow: 'auto',
          padding: '16px 24px',
          display: 'flex',
          flexDirection: 'column',
          gap: 16,
        }}
      >
        {messages.length === 0 && (
          <div style={{ textAlign: 'center', marginTop: 120, color: '#666' }}>
            <h2 style={{ marginBottom: 16, color: '#aaa' }}>Z-Plan A 股量化助手</h2>
            <p>输入股票代码或名称进行分析</p>
            <p>问"选股"获取今日推荐榜单</p>
            <p>问"最新"获取市场资讯</p>
            <Space style={{ marginTop: 16 }}>
              {['分析 爱普股份', '选股', '最新', '筛选 AI'].map((q) => (
                <Tag
                  key={q}
                  style={{ cursor: 'pointer', padding: '4px 12px' }}
                  color="blue"
                  onClick={() => {
                    setInput(q)
                    setTimeout(() => send(), 50)
                  }}
                >
                  {q}
                </Tag>
              ))}
            </Space>
          </div>
        )}

        {messages.map((msg, idx) => (
          <div
            key={idx}
            style={{
              display: 'flex',
              justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start',
            }}
          >
            <Card
              size="small"
              style={{
                maxWidth: '80%',
                background: msg.role === 'user' ? '#1677ff' : '#1f1f1f',
                border: msg.role === 'user' ? 'none' : '1px solid #303030',
              }}
              styles={{ body: { padding: '10px 16px' } }}
            >
              {msg.role === 'assistant' ? (
                <div style={{ lineHeight: 1.8 }}>
                  <ReactMarkdown>{msg.content || (msg.streaming ? '▊' : '')}</ReactMarkdown>
                  {msg.chart && (
                    <div style={{ marginTop: 12, borderTop: '1px solid #303030', paddingTop: 12 }}>
                      <div style={{ marginBottom: 8, fontSize: 12, color: '#888' }}>
                        📈 技术趋势图
                      </div>
                      <Image
                        src={msg.chart.chart_url}
                        alt="K线图"
                        style={{ maxWidth: '100%', maxHeight: 400, objectFit: 'contain', background: '#0d0d0d', borderRadius: 4 }}
                        preview={{ mask: '点击放大' }}
                      />
                      <div style={{ marginTop: 8 }}>
                        <Button size="small" onClick={() => navigate(msg.chart!.detail_url)}>
                          查看个股详情 →
                        </Button>
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div style={{ color: '#fff' }}>{msg.content}</div>
              )}
              {msg.cost_usd != null && msg.cost_usd > 0 && (
                <div style={{ fontSize: 11, color: '#888', marginTop: 4 }}>${msg.cost_usd.toFixed(5)}</div>
              )}
            </Card>
          </div>
        ))}
      </div>

      {/* 输入栏 */}
      <div style={{ padding: '12px 24px', borderTop: '1px solid #303030' }}>
        <Space.Compact style={{ width: '100%' }}>
          <Input
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onPressEnter={send}
            placeholder="输入股票代码/名称，或提问（如：分析 爱普股份、选股、最新）"
            disabled={loading}
            size="large"
            style={{ background: '#1f1f1f', borderColor: '#303030' }}
          />
          <Button
            type="primary"
            icon={<SendOutlined />}
            onClick={send}
            loading={loading}
            size="large"
          >
            发送
          </Button>
        </Space.Compact>
      </div>
    </div>
  )
}
