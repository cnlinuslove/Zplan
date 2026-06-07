import { Routes, Route } from 'react-router-dom'
import { Layout } from 'antd'
import MainLayout from './components/MainLayout'
import ChatPage from './pages/ChatPage'
import PicksPage from './pages/PicksPage'
import PickDetailPage from './pages/PickDetailPage'
import WatchlistPage from './pages/WatchlistPage'
import MarketPage from './pages/MarketPage'
import StockDetailPage from './pages/StockDetailPage'
import DashboardPage from './pages/DashboardPage'

function App() {
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <MainLayout>
        <Routes>
          <Route path="/" element={<ChatPage />} />
          <Route path="/picks" element={<PicksPage />} />
          <Route path="/picks/:entryId" element={<PickDetailPage />} />
          <Route path="/watchlist" element={<WatchlistPage />} />
          <Route path="/market" element={<MarketPage />} />
          <Route path="/market/:tsCode" element={<StockDetailPage />} />
          <Route path="/dashboard" element={<DashboardPage />} />
        </Routes>
      </MainLayout>
    </Layout>
  )
}

export default App
