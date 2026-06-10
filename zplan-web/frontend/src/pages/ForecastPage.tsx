import { useQuery } from '@tanstack/react-query'
import { Card, Tag, Typography, Collapse, Spin, Empty, Statistic, Row, Col, Table, Progress, Image, Timeline } from 'antd'
import {
  RiseOutlined, FallOutlined, MinusOutlined, CheckCircleOutlined,
  CloseCircleOutlined, ThunderboltOutlined, HistoryOutlined,
  SafetyOutlined, AimOutlined, PictureOutlined, WarningOutlined,
  LineChartOutlined,
} from '@ant-design/icons'
import api from '../api/client'

const { Title, Text, Paragraph } = Typography

const DIRECTION_MAP: Record<string, { color: string; icon: React.ReactNode; label: string }> = {
  bullish: { color: 'red', icon: <RiseOutlined />, label: '看涨' },
  bearish: { color: 'green', icon: <FallOutlined />, label: '看跌' },
  'range-bound': { color: 'orange', icon: <MinusOutlined />, label: '震荡' },
}

const SECTOR_DIRECTION_MAP: Record<string, string> = {
  '看多': 'green',
  '看淡': 'red',
  '中性': 'orange',
}

const INDEX_NAMES: Record<string, string> = {
  '000001': '上证指数', '399001': '深证成指', '399006': '创业板指',
  '000688': '科创50', '000300': '沪深300', '000905': '中证500', '000852': '中证1000',
}

export default function ForecastPage() {
  const { data: forecast, isLoading } = useQuery({
    queryKey: ['forecast-latest'],
    queryFn: () => api.get('/forecast/latest'),
    refetchInterval: 60_000,
  })

  const { data: history } = useQuery({
    queryKey: ['forecast-history'],
    queryFn: () => api.get('/forecast/history', { params: { days: 30 } }),
  })

  const f = forecast?.data?.forecast
  const h = history?.data
  const fd = f?.forecast_data
  const charts = f?.charts

  if (isLoading) return <Spin style={{ display: 'block', margin: '100px auto' }} />
  if (!f) return <Empty style={{ marginTop: 100 }} description="暂无预测数据" />

  const md = fd?.market_direction || {}
  const dir = DIRECTION_MAP[md.direction] || DIRECTION_MAP['range-bound']
  const verified = f?.verified
  const hasEnoughData = (h?.stats?.total_verified || 0) >= 3

  return (
    <div style={{ padding: 24, height: '100vh', overflow: 'auto' }}>
      <Title level={4}>🔮 大盘预测</Title>

      {/* ── 综合判断卡片 ── */}
      <Card style={{ marginBottom: 16 }}>
        <Row gutter={[24, 16]} align="middle">
          <Col xs={24} md={8}>
            <Statistic
              title="大盘方向"
              valueRender={() => (
                <Tag color={dir.color} style={{ fontSize: 24, padding: '4px 16px' }}>
                  {dir.icon} {dir.label}
                </Tag>
              )}
            />
          </Col>
          <Col xs={12} md={8}>
            <Statistic
              title="置信度"
              value={md.confidence || 0}
              suffix="%"
              prefix={<AimOutlined />}
            />
            <Progress
              percent={md.confidence || 0}
              strokeColor={md.confidence > 60 ? '#52c41a' : '#faad14'}
              showInfo={false}
              style={{ marginTop: 4 }}
            />
          </Col>
          <Col xs={12} md={8}>
            <Statistic
              title="预测日期"
              value={f.as_of_date}
              prefix={<HistoryOutlined />}
            />
          </Col>
        </Row>

        {md.reasoning && (
          <Paragraph style={{ marginTop: 16, padding: 12, background: '#fafafa', borderRadius: 8 }}>
            <Text strong>综合判断：</Text>{md.reasoning}
          </Paragraph>
        )}

        {/* 证据链 — 默认展开 */}
        <Row gutter={[16, 12]} style={{ marginTop: 16 }}>
          {(md.evidence || []).length > 0 && (
            <Col xs={24} md={12}>
              <Card size="small" title={<span style={{ color: '#52c41a' }}>✅ 看多证据</span>}>
                {md.evidence.map((e: any, i: number) => (
                  <div key={i} style={{ padding: '4px 0', paddingLeft: 12, borderLeft: '3px solid #52c41a', marginTop: 4 }}>
                    <Tag color="blue" style={{ marginBottom: 4 }}>{e.type}</Tag>
                    <Text style={{ display: 'block', fontSize: 13 }}>{e.signal}：<Text code>{e.value}</Text></Text>
                  </div>
                ))}
              </Card>
            </Col>
          )}
          {(md.counter_evidence || []).length > 0 && (
            <Col xs={24} md={12}>
              <Card size="small" title={<span style={{ color: '#ff4d4f' }}>⚠️ 反向风险</span>}>
                {md.counter_evidence.map((c: string, i: number) => (
                  <div key={i} style={{ padding: '4px 0', paddingLeft: 12, borderLeft: '3px solid #ff4d4f', marginTop: 4 }}>
                    <Tag color="error" style={{ marginBottom: 4 }}>风险</Tag>
                    <Text style={{ fontSize: 13 }}>{c}</Text>
                  </div>
                ))}
              </Card>
            </Col>
          )}
        </Row>
      </Card>

      {/* ── 技术图表（折叠区）── */}
      {charts && Object.keys(charts).length > 0 && (
        <Card title={<span><PictureOutlined /> 技术图表</span>} style={{ marginBottom: 16 }}>
          <Collapse
            accordion
            items={[
              // 热力图置顶
              ...(charts.sector_heatmap ? [{
                key: 'sector_heatmap',
                label: '🔥 板块热力图',
                children: (
                  <Image
                    src={charts.sector_heatmap}
                    alt="板块热力图"
                    style={{ width: '100%', maxHeight: 500, objectFit: 'contain', borderRadius: 8 }}
                    fallback="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
                  />
                ),
              }] : []),
              // 各指数图表
              ...Object.entries(charts)
                .filter(([code]) => code !== 'sector_heatmap')
                .map(([code, paths]) => ({
                  key: code,
                  label: `${INDEX_NAMES[code] || code} (${code})`,
                  children: (
                    <Row gutter={[12, 12]}>
                      {Object.entries((paths || {}) as Record<string, string>).map(([type, url]) => (
                        <Col xs={24} md={12} key={type}>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {type === 'kline' ? '📈 K线+均线' : '📉 MACD+形态'}
                          </Text>
                          <Image
                            src={url}
                            alt={`${code} ${type}`}
                            style={{ width: '100%', borderRadius: 8, border: '1px solid #f0f0f0' }}
                            fallback="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
                          />
                        </Col>
                      ))}
                    </Row>
                  ),
                })),
            ]}
          />
        </Card>
      )}

      {/* ── 验证对照 ── */}
      {verified && (
        <Card
          title={<span><CheckCircleOutlined /> 预测验证</span>}
          style={{ marginBottom: 16 }}
          extra={
            verified.direction_correct
              ? <Tag color="success">方向正确 ✅</Tag>
              : <Tag color="error">方向偏差 ❌</Tag>
          }
        >
          <Row gutter={16}>
            <Col span={8}>
              <Statistic title="预测方向" value={md.direction} />
            </Col>
            <Col span={8}>
              <Statistic
                title="实际方向"
                value={verified.actual_direction}
                suffix={verified.actual_pct_chg != null ? `(${verified.actual_pct_chg > 0 ? '+' : ''}${verified.actual_pct_chg}%)` : ''}
              />
            </Col>
            <Col span={8}>
              <Statistic
                title="验证时间"
                value={verified.at?.split('T')[0] || '-'}
              />
            </Col>
          </Row>
        </Card>
      )}

      {/* ── 指数预测 + 情景推演（两栏）── */}
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        {fd?.index_forecasts?.length > 0 && (
          <Col xs={24} md={14}>
            <Card title="🏛️ 七指数预测" style={{ height: '100%' }}>
              <Table
                dataSource={fd.index_forecasts}
                rowKey="code"
                size="small"
                pagination={false}
                columns={[
                  { title: '指数', dataIndex: 'name', key: 'name', width: 90 },
                  {
                    title: '方向', dataIndex: 'direction', key: 'direction', width: 80,
                    render: (d: string) => {
                      const icon = d === '偏多' ? <RiseOutlined /> : d === '偏空' ? <FallOutlined /> : <MinusOutlined />
                      const color = d === '偏多' ? 'red' : d === '偏空' ? 'green' : 'orange'
                      return <Tag color={color}>{icon} {d}</Tag>
                    },
                  },
                  { title: '信', dataIndex: 'confidence', key: 'confidence', width: 50, render: (v: number) => `${v}%` },
                  { title: '历史相似', dataIndex: 'similar_patterns_verdict', key: 'sp', ellipsis: true },
                  {
                    title: '支撑/压力', dataIndex: 'key_levels', key: 'levels', width: 90,
                    render: (kl: any) => kl ? `${kl.support?.toFixed(0) || '?'} / ${kl.resistance?.toFixed(0) || '?'}` : '-',
                  },
                  ...(charts ? [{
                    title: '图', dataIndex: 'code', key: 'chart', width: 50,
                    render: (code: string) => {
                      const idxCharts = charts[code] as Record<string, string> | undefined
                      const macdUrl = idxCharts?.macd
                      if (!macdUrl) return null
                      return (
                        <Image
                          src={macdUrl}
                          width={40}
                          height={24}
                          style={{ objectFit: 'cover', borderRadius: 4, cursor: 'pointer' }}
                          preview={{ mask: '放大' }}
                        />
                      )
                    },
                  }] : []),
                ]}
              />
            </Card>
          </Col>
        )}
        {fd?.next_day_scenarios?.length > 0 && (
          <Col xs={24} md={10}>
            <Card title="🎯 次日情景推演" style={{ height: '100%' }}>
              {fd.next_day_scenarios.map((s: any, i: number) => (
                <div key={i} style={{ padding: '8px 0', borderBottom: i < fd.next_day_scenarios.length - 1 ? '1px solid #eee' : 'none' }}>
                  <Progress
                    percent={s.probability}
                    size="small"
                    strokeColor={s.probability > 40 ? '#1890ff' : '#faad14'}
                    format={() => `${s.probability}%`}
                    style={{ width: 80, display: 'inline-block', marginRight: 8 }}
                  />
                  <Text strong>{s.scenario}</Text>
                  <br />
                  <Text type="secondary" style={{ fontSize: 12 }}>触发条件：{s.trigger}</Text>
                </div>
              ))}
            </Card>
          </Col>
        )}
      </Row>

      {/* ── 板块判断 + 概念关注（两栏）── */}
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        {fd?.sector_calls?.length > 0 && (
          <Col xs={24} md={14}>
            <Card title="🏭 板块判断">
              <Row gutter={[8, 8]}>
                {fd.sector_calls.map((s: any, i: number) => {
                  const sc = SECTOR_DIRECTION_MAP[s.direction] || 'default'
                  const evidence = (s.evidence || []).map((e: any) => `${e.signal}: ${e.value}`).join('；')
                  return (
                    <Col xs={24} sm={12} key={i}>
                      <Card size="small" style={{ background: '#fafafa' }}>
                        <Tag color={sc}>{s.direction}</Tag>
                        <Text strong> {s.sector}</Text>
                        <Text type="secondary"> 置信{s.confidence}%</Text>
                        <Paragraph style={{ marginTop: 4, marginBottom: 0, fontSize: 13 }}>{s.reasoning}</Paragraph>
                        {evidence && <Text type="secondary" style={{ fontSize: 11 }}>📎 {evidence}</Text>}
                      </Card>
                    </Col>
                  )
                })}
              </Row>
            </Card>
          </Col>
        )}
        {fd?.concept_calls?.length > 0 && (
          <Col xs={24} md={10}>
            <Card title="💡 概念关注">
              {fd.concept_calls.map((c: any, i: number) => (
                <Tag key={i} color="purple" style={{ margin: 4, padding: '4px 10px', fontSize: 13 }}>
                  <ThunderboltOutlined /> {c.concept} ({c.direction}): {c.reasoning}
                </Tag>
              ))}
            </Card>
          </Col>
        )}
      </Row>

      {/* ── 风险因素 ── */}
      {fd?.risk_factors?.length > 0 && (
        <Card title={<span><WarningOutlined /> 风险因素</span>} style={{ marginBottom: 16 }}>
          {fd.risk_factors.map((r: string, i: number) => (
            <div key={i} style={{ padding: '4px 0' }}>
              <Tag color="error">风险 {i + 1}</Tag> {r}
            </div>
          ))}
        </Card>
      )}

      {/* ── 准确率深度分析（折叠，默认收合）── */}
      {h && (
        <Collapse
          style={{ marginBottom: 16 }}
          items={[{
            key: 'accuracy',
            label: <span><LineChartOutlined /> 预测准确率分析 {h.stats?.total_verified ? `(${h.stats.total_verified}次验证)` : ''}</span>,
            children: (
              <div>
                {!hasEnoughData && (
                  <Paragraph type="secondary" style={{ textAlign: 'center', padding: 16 }}>
                    ⚠️ 数据不足（需 ≥ 3 次验证），统计结果仅供参考
                  </Paragraph>
                )}
                <Row gutter={16} style={{ marginBottom: 16 }}>
                  <Col span={6}>
                    <Statistic title="已验证" value={h.stats?.total_verified || 0} suffix="次" />
                  </Col>
                  <Col span={6}>
                    <Statistic title="方向正确" value={h.stats?.correct || 0} suffix="次" valueStyle={{ color: '#52c41a' }} />
                  </Col>
                  <Col span={6}>
                    <Statistic
                      title="准确率"
                      value={h.stats?.accuracy_pct || 0}
                      suffix="%"
                      prefix={h.stats?.accuracy_pct > 50 ? <CheckCircleOutlined /> : <CloseCircleOutlined />}
                    />
                  </Col>
                </Row>

                {/* 方向对称性 */}
                {h.stats?.by_direction && (
                  <div style={{ marginTop: 16 }}>
                    <Text strong>方向分类准确率</Text>
                    <Table
                      dataSource={[
                        { dir: '看涨 (bullish)', ...h.stats.by_direction, key: 'bullish' },
                      ]}
                      rowKey="key"
                      size="small"
                      pagination={false}
                      columns={[
                        { title: '方向', dataIndex: 'dir' },
                        { title: '次数', dataIndex: 'bullish_count' },
                        { title: '正确', dataIndex: 'bullish_correct' },
                        { title: '准确率', dataIndex: 'bullish_accuracy', render: (v: any) => v != null ? `${v}%` : '-' },
                      ]}
                      style={{ display: 'none' }}
                    />
                    <Row gutter={8}>
                      {['bullish', 'bearish', 'range_bound'].map((d) => {
                        const count = h.stats.by_direction?.[`${d}_count`] || 0
                        const correct = h.stats.by_direction?.[`${d}_correct`] || 0
                        const pct = count > 0 ? Math.round(correct / count * 100) : 0
                        const info = DIRECTION_MAP[d === 'range_bound' ? 'range-bound' : d] || {}
                        return (
                          <Col span={8} key={d}>
                            <Card size="small">
                              <Statistic
                                title={info.label || d}
                                value={pct}
                                suffix={`% (${correct}/${count})`}
                                valueStyle={{ fontSize: 20, color: pct >= 50 ? '#52c41a' : '#ff4d4f' }}
                              />
                            </Card>
                          </Col>
                        )
                      })}
                    </Row>
                  </div>
                )}

                {/* 置信度校准 */}
                {h.stats?.confidence_calibration?.length > 0 && (
                  <div style={{ marginTop: 16 }}>
                    <Text strong>置信度校准</Text>
                    <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
                      检验 LLM 的置信度是否可靠：80% 置信度时实际准确率应当接近 80%。
                    </Paragraph>
                    {h.stats.confidence_calibration.map((bin: any, i: number) => (
                      <div key={i} style={{ marginBottom: 8 }}>
                        <Text style={{ width: 80, display: 'inline-block' }}>{bin.bin}</Text>
                        <Progress
                          percent={bin.accuracy_pct}
                          size="small"
                          style={{ width: 200, display: 'inline-block', marginRight: 8 }}
                          strokeColor={
                            bin.bin_expected && bin.accuracy_pct >= bin.bin_expected * 0.8 ? '#52c41a' :
                            bin.accuracy_pct >= 50 ? '#faad14' : '#ff4d4f'
                          }
                        />
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {bin.accurate}/{bin.count} 次
                        </Text>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ),
          }]}
        />
      )}

      {/* ── 历史预测时间线 ── */}
      {h?.history?.length > 0 && (
        <Card title="📅 历史预测" size="small">
          <Timeline
            items={h.history.slice(0, 10).map((entry: any) => ({
              color: entry.correct ? 'green' : 'red',
              dot: entry.correct ? <CheckCircleOutlined /> : <CloseCircleOutlined />,
              children: (
                <div>
                  <Text strong>{entry.as_of_date}</Text>
                  <Tag color={DIRECTION_MAP[entry.predicted]?.color}>
                    {DIRECTION_MAP[entry.predicted]?.icon} {DIRECTION_MAP[entry.predicted]?.label}
                  </Tag>
                  <Text type="secondary"> → 实际 </Text>
                  <Tag color={DIRECTION_MAP[entry.actual]?.color}>
                    {DIRECTION_MAP[entry.actual]?.icon} {DIRECTION_MAP[entry.actual]?.label}
                  </Tag>
                  {entry.actual_pct != null && (
                    <Text type="secondary" style={{ marginLeft: 8 }}>
                      ({entry.actual_pct > 0 ? '+' : ''}{entry.actual_pct}%)
                    </Text>
                  )}
                  <Tag color={entry.correct ? 'success' : 'error'} style={{ marginLeft: 8 }}>
                    {entry.correct ? '✓' : '✗'}
                  </Tag>
                  {entry.confidence != null && (
                    <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                      置信 {entry.confidence}%
                    </Text>
                  )}
                </div>
              ),
            }))}
          />
        </Card>
      )}
    </div>
  )
}
