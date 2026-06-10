import { useQuery } from '@tanstack/react-query'
import { Card, Tag, Table, Typography, Spin, Empty, Space, Badge } from 'antd'
import {
  ClockCircleOutlined,
  CheckCircleOutlined,
} from '@ant-design/icons'
import api from '../api/client'

const { Title, Text } = Typography

const ACTION_COLORS: Record<string, string> = {
  BUY_AT_OPEN: 'green',
  BUY_ON_PULLBACK: 'orange',
  WAIT_OBSERVE: 'blue',
  SKIP_TODAY: 'default',
  EXIT_SIGNAL: 'red',
}

const ACTION_LABELS: Record<string, string> = {
  BUY_AT_OPEN: '买入',
  BUY_ON_PULLBACK: '等回调',
  WAIT_OBSERVE: '观望',
  SKIP_TODAY: '放弃',
  EXIT_SIGNAL: '止损',
  '': '待定',
}

export default function ExecutionPage() {
  const { data: statusData, isLoading: statusLoading } = useQuery({
    queryKey: ['execution-status'],
    queryFn: () => api.get('/execution/status'),
    refetchInterval: 60_000, // 每分钟刷新
  })

  const { data: todayData, isLoading: planLoading } = useQuery({
    queryKey: ['execution-today'],
    queryFn: () => api.get('/execution/today'),
    refetchInterval: 120_000,
  })

  const status = statusData?.data
  const today = todayData?.data

  if (statusLoading || planLoading) return <Spin style={{ display: 'block', margin: '40vh auto' }} />

  const stages = status?.stages || []
  const plans = today?.plans || []
  const summary = today?.summary

  return (
    <div style={{ padding: 24, height: '100vh', overflow: 'auto', maxWidth: 1200, margin: '0 auto' }}>
      <Title level={2}>📡 执行看板</Title>

      {/* 状态时间线 */}
      <Card title="⏱ 今日时间线" style={{ marginBottom: 16 }}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Text type="secondary">
            {status?.date} · {status?.is_weekend ? '🔴 周末休市' : status?.has_snapshot ? '🟢 数据就绪' : '🟡 等待数据'}
          </Text>
          <div style={{ display: 'flex', gap: 16, marginTop: 8 }}>
            {stages.map((s: any) => (
              <Badge
                key={s.key}
                status={s.done ? 'success' : 'default'}
                text={
                  <span>
                    {s.done ? <CheckCircleOutlined style={{ color: '#52c41a' }} /> : <ClockCircleOutlined />}
                    {' '}{s.label} <Text type="secondary" style={{ fontSize: 11 }}>{s.time}</Text>
                  </span>
                }
              />
            ))}
          </div>
        </Space>
      </Card>

      {/* 汇总面板 */}
      {summary && summary.total > 0 && (
        <Card title="📊 今日概览" style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
            <div>
              <Text type="secondary">推荐总数</Text>
              <div><Title level={4} style={{ margin: 0 }}>{summary.total}</Title></div>
            </div>
            <div>
              <Text type="secondary">可买入</Text>
              <div><Title level={4} style={{ margin: 0, color: '#52c41a' }}>{summary.buy_now}</Title></div>
            </div>
            <div>
              <Text type="secondary">等回调</Text>
              <div><Title level={4} style={{ margin: 0, color: '#faad14' }}>{summary.buy_on_pullback}</Title></div>
            </div>
            <div>
              <Text type="secondary">观望</Text>
              <div><Title level={4} style={{ margin: 0, color: '#1677ff' }}>{summary.wait}</Title></div>
            </div>
            <div>
              <Text type="secondary">放弃</Text>
              <div><Title level={4} style={{ margin: 0 }}>{summary.skip}</Title></div>
            </div>
            <div>
              <Text type="secondary">触及目标</Text>
              <div><Title level={4} style={{ margin: 0, color: '#52c41a' }}>{summary.hit_target}</Title></div>
            </div>
            <div>
              <Text type="secondary">触及止损</Text>
              <div><Title level={4} style={{ margin: 0, color: '#ff4d4f' }}>{summary.hit_stop}</Title></div>
            </div>
          </div>
        </Card>
      )}

      {/* 操作清单 */}
      <Card title="📋 今日操作清单" style={{ marginBottom: 16 }}>
        {plans.length === 0 ? (
          <Empty description="今日暂无执行计划数据" />
        ) : (
          <Table
            dataSource={plans}
            rowKey="ts_code"
            size="small"
            pagination={false}
            columns={[
              { title: '#', dataIndex: 'rank', width: 40 },
              {
                title: '标的', key: 'name', width: 140,
                render: (_: any, r: any) => (
                  <a href={`/market/${r.ts_code}`} target="_blank">
                    <strong>{r.name}</strong>({r.ts_code})
                  </a>
                ),
              },
              {
                title: '收盘价', dataIndex: 'close_yesterday', width: 80,
                render: (v: number) => v ? `¥${v.toFixed(2)}` : '--',
              },
              {
                title: '建议买入', key: 'buy', width: 100,
                render: (_: any, r: any) => {
                  const buy = r.adjusted_buy || r.predicted_buy
                  const adj = r.overnight_adjustment
                  return (
                    <span>
                      {buy ? `¥${buy.toFixed(2)}` : '--'}
                      {adj && adj !== 0 ? (
                        <Text type={adj > 0 ? 'success' : 'danger'} style={{ fontSize: 10 }}>
                          {' '}{adj > 0 ? '+' : ''}{(adj * 100).toFixed(1)}%
                        </Text>
                      ) : null}
                    </span>
                  )
                },
              },
              {
                title: '竞价价', dataIndex: 'auction_price', width: 80,
                render: (v: number) => v ? `¥${v.toFixed(2)}` : '--',
              },
              {
                title: '开盘价', dataIndex: 'open_price', width: 80,
                render: (v: number) => v ? `¥${v.toFixed(2)}` : '--',
              },
              {
                title: '操作', key: 'action', width: 90,
                render: (_: any, r: any) => (
                  <Tag color={ACTION_COLORS[r.open_action] || 'default'}>
                    {ACTION_LABELS[r.open_action] || r.open_action || '待定'}
                  </Tag>
                ),
              },
              {
                title: '推荐', dataIndex: 'recommendation', width: 80,
                render: (v: string) => (
                  <Tag color={v === '强烈关注' ? 'red' : v === '关注' ? 'orange' : 'default'}>{v || '--'}</Tag>
                ),
              },
              {
                title: '目标/止损', key: 'levels', width: 140,
                render: (_: any, r: any) => (
                  <span>
                    {r.predicted_target ? <Text type="success">🎯¥{r.predicted_target?.toFixed(2)}</Text> : ''}
                    {r.predicted_target && r.predicted_stop ? ' / ' : ''}
                    {r.predicted_stop ? <Text type="danger">🛑¥{r.predicted_stop?.toFixed(2)}</Text> : ''}
                  </span>
                ),
              },
              {
                title: '状态', key: 'status', width: 80,
                render: (_: any, r: any) => {
                  if (r.hit_target) return <Tag color="green">🎯 已止盈</Tag>
                  if (r.hit_stop) return <Tag color="red">🛑 已止损</Tag>
                  if (r.open_action === 'BUY_AT_OPEN') return <Tag color="green">已买入</Tag>
                  return <Tag>待执行</Tag>
                },
              },
            ]}
          />
        )}
      </Card>

      {/* 盘中信号 */}
      {plans.some((p: any) => (p.intraday_notes || []).length > 0) && (
        <Card title="📡 盘中信号" style={{ marginBottom: 16 }}>
          {plans.filter((p: any) => (p.intraday_notes || []).length > 0).map((p: any) => (
            <div key={p.ts_code} style={{ marginBottom: 8 }}>
              <Text strong>{p.name}({p.ts_code})</Text>
              {(p.intraday_notes || []).map((note: string, i: number) => (
                <div key={i} style={{ marginLeft: 16 }}>{note}</div>
              ))}
            </div>
          ))}
        </Card>
      )}

      {/* 盘前备注 */}
      {plans.some((p: any) => (p.pre_market_notes || []).length > 0) && (
        <Card title="🔔 盘前关注" style={{ marginBottom: 16 }}>
          {plans.filter((p: any) => (p.pre_market_notes || []).some((n: string) => n.includes('⚠️'))).map((p: any) => (
            <div key={p.ts_code} style={{ marginBottom: 8 }}>
              <Text strong>{p.name}({p.ts_code})</Text>
              {(p.pre_market_notes || []).map((note: string, i: number) => (
                <div key={i} style={{ marginLeft: 16, color: note.includes('⚠️') ? '#ff4d4f' : undefined }}>{note}</div>
              ))}
            </div>
          ))}
        </Card>
      )}
    </div>
  )
}
