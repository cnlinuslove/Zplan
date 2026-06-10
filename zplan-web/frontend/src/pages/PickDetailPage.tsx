import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Card, Descriptions, Tag, Button, Space, Typography, Spin, List, Image } from 'antd'
import ResearchButton from '../components/ResearchButton'
import { ArrowLeftOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import api from '../api/client'

const { Title, Text } = Typography

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
  const signals: string[] = analysis.signals || []
  const llmBrief = analysis.llm_brief || {}
  const ret20d = analysis.ret_20d
  const high60d = analysis.high_60d_pct
  const kdj = analysis.kdj || {}

  const recColors: Record<string, string> = {
    '强烈关注': 'red', '关注': 'orange', '观望': 'default', '谨慎': 'blue', '回避': 'default',
  }

  // 判断颜色
  const scoreColor = (s: number) => s >= 70 ? '#52c41a' : s >= 55 ? '#faad14' : '#ff4d4f'

  return (
    <div style={{ padding: 24, height: '100vh', overflow: 'auto' }}>
      <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(-1)} style={{ marginBottom: 16 }}>
        返回
      </Button>

      {/* 头部信息 */}
      <Card style={{ marginBottom: 16 }}>
        <Space align="center" style={{ marginBottom: 8 }}>
          <Title level={3} style={{ margin: 0 }}>
            {e.name} ({e.ts_code})
          </Title>
          <Tag color={recColors[e.recommendation] || 'default'}>{e.recommendation || '-'}</Tag>
          <Tag>{e.verdict || '-'}</Tag>
          <Button
            size="small"
            onClick={() => navigate(`/market/${e.ts_code}`)}
          >
            查看行情
          </Button>
        </Space>

        <Descriptions bordered size="small" column={4}>
          <Descriptions.Item label="排名">#{e.rank}</Descriptions.Item>
          <Descriptions.Item label="收盘价">{e.close_price?.toFixed(2)}</Descriptions.Item>
          <Descriptions.Item label="规则综合分">
            <Text strong style={{ color: scoreColor(e.rule_composite_score || 0) }}>
              {e.rule_composite_score?.toFixed(1)}
            </Text>
          </Descriptions.Item>
          <Descriptions.Item label="LLM综合分">{e.llm_composite_score?.toFixed(1) || '-'}</Descriptions.Item>
          <Descriptions.Item label="最终得分">
            <Text strong style={{ color: scoreColor(e.final_composite_score || 0), fontSize: 16 }}>
              {e.final_composite_score?.toFixed(1)}
            </Text>
          </Descriptions.Item>
          <Descriptions.Item label="建议买入">{e.predicted_buy_price?.toFixed(2)}</Descriptions.Item>
          <Descriptions.Item label="目标价">{e.predicted_target_price?.toFixed(2)}</Descriptions.Item>
          <Descriptions.Item label="止损价">{e.predicted_stop_loss?.toFixed(2)}</Descriptions.Item>
        </Descriptions>
      </Card>

      {/* 价格预测卡片 */}
      {e.predicted_buy_price && (
        <Card title="💹 价格预测" size="small" style={{ marginBottom: 16 }}>
          <Space size="large">
            <div>
              <Text type="secondary">建议买入</Text>
              <div><Text strong style={{ fontSize: 18, color: '#52c41a' }}>{e.predicted_buy_price?.toFixed(2)}</Text></div>
            </div>
            <div>
              <Text type="secondary">目标价</Text>
              <div><Text strong style={{ fontSize: 18, color: '#1890ff' }}>{e.predicted_target_price?.toFixed(2)}</Text></div>
            </div>
            <div>
              <Text type="secondary">止损价</Text>
              <div><Text strong style={{ fontSize: 18, color: '#ff4d4f' }}>{e.predicted_stop_loss?.toFixed(2)}</Text></div>
            </div>
            <div>
              <Text type="secondary">潜在收益</Text>
              <div><Text strong style={{ fontSize: 18, color: '#52c41a' }}>
                {e.predicted_buy_price && e.predicted_target_price
                  ? (((e.predicted_target_price - e.predicted_buy_price) / e.predicted_buy_price) * 100).toFixed(1) + '%'
                  : '-'}
              </Text></div>
            </div>
          </Space>
        </Card>
      )}

      {/* 技术趋势图 */}
      <Card title="📈 技术趋势图 (K线 + 均线 + 信号)" style={{ marginBottom: 16 }}>
        <Image
          src={`/api/v1/market/stocks/${e.ts_code}/chart?lookback=120`}
          alt="K线图"
          style={{ width: '100%', maxHeight: 700, objectFit: 'contain', background: '#0d0d0d', minHeight: 200 }}
          preview={{ mask: '点击放大' }}
          placeholder={
            <div style={{ padding: 60, textAlign: 'center', color: '#888', background: '#0d0d0d', minHeight: 200 }}>
              <Spin /> <span style={{ marginLeft: 12 }}>趋势图生成中...</span>
            </div>
          }
          fallback="https://via.placeholder.com/800x400/1a1a2e/888?text=Chart+Loading..."
        />
      </Card>

      {/* MACD + 相似历史形态画廊 */}
      <Card title="🔍 MACD 趋势 + 相似历史形态" style={{ marginBottom: 16 }}>
        <Image
          src={`/api/v1/market/stocks/${e.ts_code}/chart-macd?lookback=120`}
          alt="MACD与相似形态"
          style={{ width: '100%', maxHeight: 700, objectFit: 'contain', background: '#0d0d0d', minHeight: 200 }}
          preview={{ mask: '点击放大' }}
          placeholder={
            <div style={{ padding: 60, textAlign: 'center', color: '#888', background: '#0d0d0d', minHeight: 200 }}>
              <Spin /> <span style={{ marginLeft: 12 }}>MACD 图生成中...</span>
            </div>
          }
          fallback="https://via.placeholder.com/800x400/1a1a2e/888?text=Chart+Loading..."
        />
      </Card>

      {/* 技术信号 */}
      {signals.length > 0 && (
        <Card title="🔧 技术信号" size="small" style={{ marginBottom: 16 }}>
          <List
            size="small"
            dataSource={signals}
            renderItem={(s: string) => (
              <List.Item>
                <Tag color={s.includes('空头') || s.includes('死叉') ? 'red' : 'green'}>{s}</Tag>
              </List.Item>
            )}
          />
          {(ret20d != null || high60d != null) && (
            <div style={{ marginTop: 8 }}>
              {ret20d != null && <Tag>20日涨幅: {ret20d.toFixed(1)}%</Tag>}
              {high60d != null && <Tag>60日高位: {(high60d * 100).toFixed(0)}%</Tag>}
              {kdj.k != null && <Tag>KDJ-K: {kdj.k?.toFixed(1)}</Tag>}
            </div>
          )}
        </Card>
      )}

      {/* LLM 简评 */}
      {llmBrief && Object.keys(llmBrief).length > 0 && (
        <Card title="🤖 LLM 简评" size="small" style={{ marginBottom: 16 }}>
          {llmBrief.recommendation && <Tag color={recColors[llmBrief.recommendation] || 'default'}>{llmBrief.recommendation}</Tag>}
          {llmBrief.risks && <p style={{ marginTop: 8 }}>⚠️ 风险: {llmBrief.risks}</p>}
          {llmBrief.opportunities && <p>💡 机会: {llmBrief.opportunities}</p>}
          {llmBrief.summary && <p>📋 {llmBrief.summary}</p>}
          <pre style={{ fontSize: 11, color: '#888', marginTop: 8, whiteSpace: 'pre-wrap' }}>
            {JSON.stringify(llmBrief, null, 2)}
          </pre>
        </Card>
      )}

      {/* 回测结果 */}
      {outcomes.length > 0 && (
        <Card title="📊 回测结果" style={{ marginBottom: 16 }}>
          {outcomes.map((o: any) => (
            <Tag key={o.horizon_days} color={o.hit_target ? 'green' : o.hit_stop ? 'red' : 'default'} style={{ margin: 4, padding: '4px 10px' }}>
              {o.horizon_days}天 收益 {o.return_pct?.toFixed(2)}%
              {o.hit_buy ? ' ✅买入' : ' ❌未触及'}
              {o.hit_target ? ' 🎯达标' : ''}
              {o.hit_stop ? ' 🛑止损' : ''}
            </Tag>
          ))}
        </Card>
      )}

      {/* LLM 深度研报 Markdown */}
      {e.markdown_report ? (
        <Card title="📝 LLM 深度研报" style={{ marginBottom: 16 }}>
          <div style={{ lineHeight: 1.8 }}>
            <ReactMarkdown>{e.markdown_report}</ReactMarkdown>
          </div>
        </Card>
      ) : (
        <Card title="📝 选股分析摘要" style={{ marginBottom: 16 }}>
          <List size="small">
            <List.Item>
              <Text strong>综合评分：</Text>
              <Text style={{ fontSize: 18, color: scoreColor(e.final_composite_score || 0) }}>
                {e.final_composite_score?.toFixed(1)}
              </Text>
              <Tag style={{ marginLeft: 8 }}>{e.verdict || '-'}</Tag>
              <Tag color={recColors[e.recommendation] || 'default'}>{e.recommendation || '-'}</Tag>
            </List.Item>
            {e.close_price && e.predicted_buy_price && (
              <List.Item>
                <Text>当前价 {e.close_price.toFixed(2)}，建议买入价 {e.predicted_buy_price.toFixed(2)}，
                  目标价 {e.predicted_target_price?.toFixed(2)}（
                  <Text style={{ color: '#52c41a' }}>
                    +{(((e.predicted_target_price - e.predicted_buy_price) / e.predicted_buy_price) * 100).toFixed(1)}%
                  </Text>）
                </Text>
              </List.Item>
            )}
            {signals.length > 0 && (
              <List.Item>
                <Text>信号：{signals.join(' · ')}</Text>
              </List.Item>
            )}
            {llmBrief?.summary && (
              <List.Item>
                <Text>📋 {llmBrief.summary}</Text>
              </List.Item>
            )}
            {llmBrief?.risks && (
              <List.Item>
                <Text type="danger">⚠️ 风险：{llmBrief.risks}</Text>
              </List.Item>
            )}
          </List>
          {!e.markdown_report && (
            <div style={{ marginTop: 12 }}>
              <Space>
                <Text type="secondary">
                  💡 该条目为规则扫描结果，暂无 LLM 深度研报。
                </Text>
                <ResearchButton tsCode={e.ts_code} />
              </Space>
            </div>
          )}
        </Card>
      )}

      {/* 分析过程 JSON（折叠） */}
      {Object.keys(analysis).length > 0 && (
        <Card title="🔍 完整分析数据" size="small">
          <pre style={{ fontSize: 11, overflow: 'auto', maxHeight: 300, color: '#888' }}>
            {JSON.stringify(analysis, null, 2)}
          </pre>
        </Card>
      )}
    </div>
  )
}
