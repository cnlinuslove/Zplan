import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Table, Button, Input, Space, Card, Typography, Modal, message } from 'antd'
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons'
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

  const { data, isLoading } = useQuery({
    queryKey: ['watchlist'],
    queryFn: () => api.get('/watchlist'),
    refetchInterval: 60_000,
  })

  const addMutation = useMutation({
    mutationFn: (body: { ts_code: string; notes?: string }) => api.post('/watchlist', body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
      setOpen(false)
      setNewCode('')
      setNewNote('')
    },
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

      <Table
        columns={[
          { title: '代码', dataIndex: 'ts_code', key: 'ts_code', width: 100 },
          { title: '名称', dataIndex: 'name', key: 'name', width: 120 },
          { title: '行业', dataIndex: 'industry', key: 'industry', width: 120 },
          { title: '备注', dataIndex: 'notes', key: 'notes' },
          {
            title: '操作',
            key: 'actions',
            width: 100,
            render: (_: any, r: WatchItem) => (
              <Space>
                <Button size="small" onClick={() => navigate(`/market/${r.ts_code}`)}>详情</Button>
                <Button
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => removeMutation.mutate(r.ts_code)}
                />
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

      <Modal
        title="添加自选股"
        open={open}
        onOk={() => addMutation.mutate({ ts_code: newCode, notes: newNote })}
        onCancel={() => setOpen(false)}
        okText="添加"
        cancelText="取消"
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input
            placeholder="股票代码（如 600519）"
            value={newCode}
            onChange={(e) => setNewCode(e.target.value)}
          />
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
