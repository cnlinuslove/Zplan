import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Card, Descriptions, Tag, Button, Space, Typography, Spin } from 'antd'
import { ArrowLeftOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import api from '../api/client'

const { Title } = Typography

export default function PickDetailPage() {
  const { entryId } = useParams()
  const navigate = useNavigate()

  const { data, isLoading } = useQuery({
    queryKey: ['pick-entry', entryId],
    queryFn: () => api.get(`/picks/entries/${entryId}`),
    enabled: !!entryId,
  })

  if (isLoading) return <Spin style={{ display: 'block', margin: '40vh auto' }} />
  if (!data?.data?.entry) return <div style={{ padding: 24 }}>未找到数据</div>

  const e = data.data.entry
  const outcomes = data.data.backtest_outcomes || []
  const analysis = e.analysis_json || {}

  const recColors: Record<string, string> = {
    '强烈关注': 'red', '关注': 'orange', '观望': 'default', '谨慎': 'blue', '回避': 'default',
  }

  return (
    <div style={{ padding: 24, height: '100vh', overflow: 'auto' }}>
      <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(-1)} style={{ marginBottom: 16 }}>
        返回
      </Button>

      <Card style={{ marginBottom: 16 }}>
        <Space align="center" style={{ marginBottom: 8 }}>
          <Title level={3} style={{ margin: 0 }}>
            {e.name} ({e.ts_code})
          </Title>
          <Tag color={recColors[e.recommendation] || 'default'}>{e.recommendation}</Tag>
          <Tag>{e.verdict}</Tag>
        </Space>

        <Descriptions bordered size="small" column={4}>
          <Descriptions.Item label="排名">#{e.rank}</Descriptions.Item>
          <Descriptions.Item label="收盘价">{e.close_price?.toFixed(2)}</Descriptions.Item>
          <Descriptions.Item label="规则综合分">{e.rule_composite_score?.toFixed(1)}</Descriptions.Item>
          <Descriptions.Item label="LLM综合分">{e.llm_composite_score?.toFixed(1) || '-'}</Descriptions.Item>
          <Descriptions.Item label="最终分">{e.final_composite_score?.toFixed(1)}</Descriptions.Item>
          <Descriptions.Item label="建议买入">{e.predicted_buy_price?.toFixed(2)}</Descriptions.Item>
          <Descriptions.Item label="目标价">{e.predicted_target_price?.toFixed(2)}</Descriptions.Item>
          <Descriptions.Item label="止损价">{e.predicted_stop_loss?.toFixed(2)}</Descriptions.Item>
        </Descriptions>
      </Card>

      {/* 回测结果 */}
      {outcomes.length > 0 && (
        <Card title="📊 回测结果" style={{ marginBottom: 16 }}>
          {outcomes.map((o: any) => (
            <Tag key={o.horizon_days} color={o.hit_target ? 'green' : o.hit_stop ? 'red' : 'default'}>
              {o.horizon_days}天 收益 {o.return_pct?.toFixed(2)}%
              {o.hit_buy ? ' ✅买入' : ' ❌未触及'}
              {o.hit_target ? ' 🎯达标' : ''}
              {o.hit_stop ? ' 🛑止损' : ''}
            </Tag>
          ))}
        </Card>
      )}

      {/* LLM 研报 Markdown */}
      {e.markdown_report && (
        <Card title="📝 LLM 深度研报" style={{ marginBottom: 16 }}>
          <div style={{ lineHeight: 1.8 }}>
            <ReactMarkdown>{e.markdown_report}</ReactMarkdown>
          </div>
        </Card>
      )}

      {/* 分析过程 JSON */}
      {Object.keys(analysis).length > 0 && (
        <Card title="🔍 规则分析细节">
          <pre style={{ fontSize: 12, overflow: 'auto', maxHeight: 400 }}>
            {JSON.stringify(analysis, null, 2)}
          </pre>
        </Card>
      )}
    </div>
  )
}
