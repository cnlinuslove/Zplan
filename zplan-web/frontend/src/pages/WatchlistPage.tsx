import { useState, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Table, Button, Input, Space, Card, Typography, Modal, AutoComplete, message, Tag, Descriptions, Empty } from 'antd'
import { PlusOutlined, DeleteOutlined, SearchOutlined } from '@ant-design/icons'
import api from '../api/client'

const { Title } = Typography

interface WatchItem {
  ts_code: string
  name: string
  industry: string
  notes: string
  enabled: boolean
}

export default function WatchlistPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [newCode, setNewCode] = useState('')
  const [newNote, setNewNote] = useState('')
  const [searchOptions, setSearchOptions] = useState<{ value: string; label: string; stock: any }[]>([])
  const [selectedStock, setSelectedStock] = useState<any>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['watchlist'],
    queryFn: () => api.get('/watchlist'),
    refetchInterval: 60_000,
  })

  // 搜索股票自动补全
  const searchStocks = useCallback(async (keyword: string) => {
    if (keyword.length < 1) { setSearchOptions([]); return }
    try {
      const res = await api.get('/market/stocks', { params: { q: keyword, limit: 10 } })
      const stocks = res.data?.stocks || []
      setSearchOptions(stocks.map((s: any) => ({
        value: s.ts_code,
        label: `${s.name} (${s.ts_code}) · ${s.industry || '-'}`,
        stock: s,
      })))
    } catch { setSearchOptions([]) }
  }, [])

  const onSelectStock = (value: string, option: any) => {
    setNewCode(value)
    setSelectedStock(option.stock)
  }

  const addMutation = useMutation({
    mutationFn: (body: { ts_code: string; notes?: string }) => api.post('/watchlist', body),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
      setOpen(false)
      setNewCode('')
      setNewNote('')
      setSelectedStock(null)
      setSearchOptions([])
      const action = res.data?.action
      if (action === 'already_exists') message.info('该股票已在自选列表中')
      else message.success(`已添加 ${selectedStock?.name || newCode}`)
    },
    onError: () => message.error('添加失败，请检查股票代码'),
  })

  const removeMutation = useMutation({
    mutationFn: (code: string) => api.delete(`/watchlist/${code}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
      message.success('已移除')
    },
  })

  const items: WatchItem[] = data?.data?.items || []

  return (
    <div style={{ padding: 24, height: '100vh', overflow: 'auto' }}>
      <Card style={{ marginBottom: 16 }}>
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Title level={4} style={{ margin: 0 }}>⭐ 自选股</Title>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
            添加
          </Button>
        </Space>
      </Card>

      {items.length === 0 && !isLoading ? (
        <Card>
          <Empty description='还没有自选股，点击「添加」添加你关注的股票' />
        </Card>
      ) : (
        <Table
          columns={[
            { title: '代码', dataIndex: 'ts_code', key: 'ts_code', width: 100 },
            { title: '名称', dataIndex: 'name', key: 'name', width: 120 },
            { title: '行业', dataIndex: 'industry', key: 'industry', width: 120 },
            { title: '备注', dataIndex: 'notes', key: 'notes' },
            {
              title: '操作',
              key: 'actions',
              width: 120,
              render: (_: any, r: WatchItem) => (
                <Space>
                  <Button size="small" onClick={(e) => { e.stopPropagation(); navigate(`/market/${r.ts_code}`) }}>详情</Button>
                  <Button size="small" danger icon={<DeleteOutlined />}
                    onClick={(e) => { e.stopPropagation(); removeMutation.mutate(r.ts_code) }} />
                </Space>
              ),
            },
          ]}
          dataSource={items}
          rowKey="ts_code"
          loading={isLoading}
          size="small"
          pagination={false}
          onRow={(record) => ({
            onClick: () => navigate(`/market/${record.ts_code}`),
            style: { cursor: 'pointer' },
          })}
        />
      )}

      <Modal
        title="添加自选股"
        open={open}
        onOk={() => {
          if (!newCode) { message.warning('请输入股票代码'); return }
          if (!selectedStock) { message.warning('请从下拉列表中选择有效股票'); return }
          addMutation.mutate({ ts_code: newCode, notes: newNote })
        }}
        onCancel={() => { setOpen(false); setSelectedStock(null); setSearchOptions([]) }}
        okText="确认添加"
        cancelText="取消"
        confirmLoading={addMutation.isPending}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <AutoComplete
            options={searchOptions}
            onSearch={searchStocks}
            onSelect={onSelectStock}
            value={newCode}
            onChange={(v) => { setNewCode(v); setSelectedStock(null) }}
            style={{ width: '100%' }}
            notFoundContent={newCode.length >= 1 ? '未找到匹配股票，请检查代码或名称' : '输入股票代码或名称搜索'}
          >
            <Input
              prefix={<SearchOutlined />}
              placeholder="输入股票代码或名称（如：600519 或 贵州茅台）"
              size="large"
            />
          </AutoComplete>

          {selectedStock && (
            <Card size="small" style={{ background: '#162312', border: '1px solid #274916' }}>
              <Descriptions size="small" column={2}>
                <Descriptions.Item label="股票">{selectedStock.name}</Descriptions.Item>
                <Descriptions.Item label="代码"><Tag>{selectedStock.ts_code}</Tag></Descriptions.Item>
                <Descriptions.Item label="行业">{selectedStock.industry || '-'}</Descriptions.Item>
                <Descriptions.Item label="市场">{selectedStock.market === 'a' ? 'A股' : '港股'}</Descriptions.Item>
              </Descriptions>
            </Card>
          )}

          <Input
            placeholder="备注（可选）"
            value={newNote}
            onChange={(e) => setNewNote(e.target.value)}
          />
        </Space>
      </Modal>
    </div>
  )
}
