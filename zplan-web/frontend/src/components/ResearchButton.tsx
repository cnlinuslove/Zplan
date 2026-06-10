import { useState } from 'react'
import { Button } from 'antd'
import api from '../api/client'

export default function ResearchButton({ tsCode, label = '🤖 生成深度研报' }: { tsCode: string; label?: string }) {
  const [loading, setLoading] = useState(false)
  const [text, setText] = useState(label)

  const handleClick = async () => {
    setLoading(true)
    setText('⏳ 启动中...')
    try {
      const res = await api.post('/picks/research', { ts_code: tsCode })
      const taskId = res.data?.task_id
      if (!taskId) {
        setText('❌ 启动失败')
        setTimeout(() => { setLoading(false); setText(label) }, 3000)
        return
      }
      for (let i = 0; i < 90; i++) {
        await new Promise(r => setTimeout(r, 2000))
        try {
          const s = (await api.get(`/tasks/${taskId}`)).data
          setText(`⏳ ${s.message || '分析中...'}`)
          if (s.status === 'completed') {
            setText('✅ 完成！')
            const entryId = s.result?.entry_id
            if (entryId) {
              window.location.href = `/picks/${entryId}`
            } else {
              window.location.reload()
            }
            return
          }
          if (s.status === 'failed') {
            setText('❌ ' + (s.message || '失败').slice(0, 30))
            setTimeout(() => { setLoading(false); setText(label) }, 5000)
            return
          }
        } catch { /* 继续轮询 */ }
      }
      setText('⏰ 超时')
      setTimeout(() => { setLoading(false); setText(label) }, 3000)
    } catch (err: any) {
      console.error('Research error:', err)
      setText('❌ ' + (err?.message || '错误').slice(0, 20))
      setTimeout(() => { setLoading(false); setText(label) }, 3000)
    }
  }

  return (
    <Button type="primary" size="small" loading={loading} onClick={handleClick}>
      {text}
    </Button>
  )
}
