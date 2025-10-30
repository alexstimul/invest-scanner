import requests
import pandas as pd
import numpy as np
import asyncio
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from datetime import datetime, timedelta
import time
from typing import Dict, List, Tuple
import logging
import os
from dotenv import load_dotenv
from helpers.TradeJournal import TradeJournal

load_dotenv()


# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class MoexScanner:
    def __init__(self, telegram_token: str, chat_id: str):
        self.moex_base_url = "https://iss.moex.com/iss"
        self.bot = telegram.Bot(token=telegram_token)
        self.chat_id = chat_id
        self.target_profit_percent = 0.01  # 1%
        self.commission = 0.001  # 0.1%
        
        # Список отслеживаемых акций (голубые фишки + волатильные)
        self.tickers = [
            'SBER', 'GAZP', 'LKOH', 'GMKN', 'ROSN', 
            'NVTK', 'TATN', 'MTSS', 'AFKS', 'PHOR',
            'PLZL', 'POLY', 'RUAL', 'MGNT', 'VTBR',
            'ALRS', 'CHMF', 'MOEX', 'YNDX', 'TCSG'
        ]
        
        # Параметры стратегии
        self.consolidation_period = 10  # дней для определения диапазона
        self.breakout_confirmation_bars = 2  # баров для подтверждения пробоя
        
    async def fetch_stock_data(self, ticker: str, timeframe: str = 'D', days_back: int = 30) -> pd.DataFrame:
        """
        Загрузка данных с Московской биржи
        timeframe: 'D' - дни, 'H1' - часы, 'M15' - 15 минут
        """
        try:
            # Определяем интервал свечей
            interval_map = {'D': 24, 'H1': 60, 'M15': 15, 'M5': 5}
            interval = interval_map.get(timeframe, 24)
            
            # Загружаем исторические данные
            url = f"{self.moex_base_url}/engines/stock/markets/shares/boards/TQBR/securities/{ticker}/candles.json"
            params = {
                'from': (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d'),
                'till': datetime.now().strftime('%Y-%m-%d'),
                'interval': interval
            }
            
            response = requests.get(url, params=params)
            data = response.json()

            if 'candles' not in data or not data['candles']['data']:
                return pd.DataFrame()
                
            df = pd.DataFrame(data['candles']['data'],
                            columns=['open', 'close', 'high', 'low', 'value', 'volume', 'begin', 'end'])
            df['begin'] = pd.to_datetime(df['begin'])
            df['end'] = pd.to_datetime(df['end'])
            df['ticker'] = ticker
            df['timeframe'] = timeframe
            
            return df.sort_values('begin').reset_index(drop=True)
            
        except Exception as e:
            logger.error(f"Error fetching data for {ticker}: {e}")
            return pd.DataFrame()
    
    def calculate_support_resistance(self, df: pd.DataFrame) -> Dict:
        """
        Расчет уровней поддержки и сопротивления
        """
        if len(df) < self.consolidation_period:
            return {}
            
        # Берем последние N свечей для анализа консолидации
        recent_data = df.tail(self.consolidation_period)
        
        # Уровень сопротивления - максимум периода
        resistance = recent_data['high'].max()
        # Уровень поддержки - минимум периода  
        support = recent_data['low'].min()
        
        # Волатильность диапазона
        range_volatility = (resistance - support) / support
        
        return {
            'support': support,
            'resistance': resistance,
            'range_volatility': range_volatility,
            'current_price': df['close'].iloc[-1],
            'consolidation_range': resistance - support
        }
    
    def detect_breakout(self, df: pd.DataFrame, levels: Dict) -> Dict:
        """
        Обнаружение пробоя уровней
        """
        if not levels or len(df) < 3:
            return {}
            
        current_price = levels['current_price']
        resistance = levels['resistance']
        support = levels['support']
        consolidation_range = levels['consolidation_range']
        
        # Минимальный размер диапазона для торговли (0.5%)
        min_acceptable_range = current_price * 0.005
        
        signal = {
            'ticker': df['ticker'].iloc[0],
            'timeframe': df['timeframe'].iloc[0],
            'current_price': current_price,
            'signal': 'HOLD',
            'signal_strength': 0,
            'stop_loss': 0,
            'take_profit': 0,
            'time_estimate': '',
            'levels': levels
        }
        
        # Проверяем, достаточно ли широкий диапазон
        if consolidation_range < min_acceptable_range:
            return signal
            
        # Анализируем последние свечи для подтверждения пробоя
        recent_closes = df['close'].tail(self.breakout_confirmation_bars).values
        recent_highs = df['high'].tail(self.breakout_confirmation_bars).values
        recent_lows = df['low'].tail(self.breakout_confirmation_bars).values
        
        # Пробой сопротивления (BUY сигнал)
        if (current_price > resistance and 
            all(close > resistance for close in recent_closes)):
            
            signal['signal'] = 'BUY'
            signal['signal_strength'] = (current_price - resistance) / consolidation_range
            
            # Расчет стоп-лосса и тейк-профита
            risk_per_share = resistance - support
            signal['stop_loss'] = support - risk_per_share * 0.1  # Чуть ниже поддержки
            signal['take_profit'] = current_price + risk_per_share * 1.5  # Risk/Reward ~1:1.5
            
            # Оценка времени удержания
            signal['time_estimate'] = self.get_time_estimate(df['timeframe'].iloc[0])
            
        # Пробой поддержки (SELL сигнал)  
        elif (current_price < support and 
              all(close < support for close in recent_closes)):
            
            signal['signal'] = 'SELL'
            signal['signal_strength'] = (support - current_price) / consolidation_range
            
            risk_per_share = resistance - support
            signal['stop_loss'] = resistance + risk_per_share * 0.1
            signal['take_profit'] = current_price - risk_per_share * 1.5
            signal['time_estimate'] = self.get_time_estimate(df['timeframe'].iloc[0])
        
        return signal
    
    def get_time_estimate(self, timeframe: str) -> str:
        """Оценка времени удержания позиции по таймфрейму"""
        estimates = {
            'D': '2-5 дней',
            'H1': '1-2 дня', 
            'M15': 'несколько часов',
            'M5': 'несколько часов'
        }
        return estimates.get(timeframe, '1-3 дня')
    
    def calculate_position_size(self, signal: Dict, capital: float = 30000) -> Dict:
        """
        Расчет размера позиции и риска
        """
        if signal['signal'] == 'HOLD':
            return signal
            
        entry_price = signal['current_price']
        stop_loss = signal['stop_loss']
        take_profit = signal['take_profit']
        
        # Риск на сделку (1% от капитала)
        risk_per_trade = capital * 0.01
        
        if signal['signal'] == 'BUY':
            risk_per_share = entry_price - stop_loss
        else:  # SELL
            risk_per_share = stop_loss - entry_price
            
        # Количество акций
        shares = int(risk_per_trade / risk_per_share) if risk_per_share > 0 else 0
        
        # Прибыль с учетом комиссий
        profit_per_share = abs(take_profit - entry_price)
        net_profit_per_share = profit_per_share * (1 - 2 * self.commission)
        
        total_profit = shares * net_profit_per_share
        profit_percent = (total_profit / capital) * 100
        
        signal.update({
            'shares': shares,
            'risk_per_trade': risk_per_trade,
            'potential_profit': total_profit,
            'potential_profit_percent': profit_percent,
            'risk_reward_ratio': profit_per_share / risk_per_share if risk_per_share > 0 else 0
        })
        
        return signal
    
    async def analyze_multi_timeframe(self, ticker: str) -> List[Dict]:
        """
        Мультитаймфреймовый анализ для одной акции
        """
        timeframes = ['D', 'H1', 'M15']
        signals = []
        
        for tf in timeframes:
            # Загружаем данные для каждого таймфрейма
            df = await self.fetch_stock_data(ticker, tf, days_back=30)
            if df.empty:
                continue
                
            # Рассчитываем уровни
            levels = self.calculate_support_resistance(df)
            if not levels:
                continue
                
            # Ищем пробой
            signal = self.detect_breakout(df, levels)
            if signal and signal['signal'] != 'HOLD':
                # Добавляем данные для фильтрации
                signal['volume'] = df['volume'].tail(3).mean()
                signal['timestamp'] = datetime.now()
                signals.append(signal)
        
        return signals
    
    def filter_strong_signals(self, signals: List[Dict]) -> List[Dict]:
        """
        Фильтрация сильных сигналов
        """
        strong_signals = []
        
        for signal in signals:
            # Минимальная сила сигнала
            if signal['signal_strength'] < 0.1:
                continue
                
            # Минимальный объем
            if signal.get('volume', 0) < 1000000:  # 1 млн руб
                continue
                
            # Risk/Reward не менее 1:1
            if signal.get('risk_reward_ratio', 0) < 1.0:
                continue
                
            strong_signals.append(signal)
            
        # Сортируем по силе сигнала
        return sorted(strong_signals, key=lambda x: x['signal_strength'], reverse=True)
    
    async def send_telegram_alert(self, signal: Dict):
        """
        Отправка сигнала в Telegram
        """
        try:
            emoji = "🟢" if signal['signal'] == 'BUY' else "🔴"
            
            message = f"""
{emoji} **ТОРГОВЫЙ СИГНАЛ** {emoji}

🎯 **Акция**: {signal['ticker']}
📊 **Таймфрейм**: {signal['timeframe']}
⚡ **Сигнал**: {signal['signal']} (сила: {signal['signal_strength']:.2f})

💰 **Текущая цена**: {signal['current_price']:.2f} ₽
🛑 **Стоп-Лосс**: {signal['stop_loss']:.2f} ₽
🎯 **Тейк-Профит**: {signal['take_profit']:.2f} ₽

📈 **Уровни**:
   - Поддержка: {signal['levels']['support']:.2f} ₽
   - Сопротивление: {signal['levels']['resistance']:.2f} ₽

💼 **Позиция**:
   - Количество акций: {signal.get('shares', 0)}
   - Потенциальная прибыль: {signal.get('potential_profit', 0):.0f} ₽ ({signal.get('potential_profit_percent', 0):.1f}%)
   - Risk/Reward: {signal.get('risk_reward_ratio', 0):.2f}

⏰ **Ожидаемое время**: {signal['time_estimate']}
🕒 **Время сигнала**: {signal['timestamp'].strftime('%H:%M %d.%m.%Y')}

⚠️ **ВНИМАНИЕ**: Это автоматический сигнал. Проверьте его на графике перед входом!
            """
            
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='Markdown'
            )
            logger.info(f"Signal sent for {signal['ticker']}")
            
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")
    
    async def scan_market(self):
        """
        Основная функция сканирования рынка
        """
        logger.info("Starting market scan...")
        all_signals = []
        
        # Анализируем все акции параллельно
        tasks = [self.analyze_multi_timeframe(ticker) for ticker in self.tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Собираем все сигналы
        for result in results:
            if isinstance(result, list):
                all_signals.extend(result)
        
        # Фильтруем и сортируем сигналы
        strong_signals = self.filter_strong_signals(all_signals)
        
        # Отправляем алерты
        for signal in strong_signals[:3]:  # Только топ-3 сигнала
            signal = self.calculate_position_size(signal)
            await self.send_telegram_alert(signal)
        
        logger.info(f"Scan completed. Found {len(strong_signals)} strong signals")
        
        return strong_signals
    
    async def run_scanner(self, interval_minutes: int = 15):
        """
        Запуск сканера с заданным интервалом
        """
        logger.info(f"Scanner started with {interval_minutes} minutes interval")
        
        while True:
            try:
                await self.scan_market()
                logger.info(f"Waiting {interval_minutes} minutes until next scan...")
                await asyncio.sleep(interval_minutes * 60)
                
            except Exception as e:
                logger.error(f"Error in scanner loop: {e}")
                await asyncio.sleep(60)  # Ждем минуту при ошибке
    
    
    async def handle_telegram_commands(self):
        """Обработка команд для ведения журнала"""
    
        # Команда для начала сделки
        @self.bot.message_handler(commands=['start_trade'])
        async def start_trade(self, message):
            # Получаем последний сигнал
            last_signals = await self.get_last_signals()
            if last_signals:
                keyboard = self.create_signal_keyboard(last_signals)
                await self.bot.send_message(message.chat.id, "Выберите сигнал для сделки:", reply_markup=keyboard)
            else:
                await self.bot.send_message(message.chat.id, "Нет активных сигналов. Дождитесь сканирования.")
        
        # Команда для записи входа
        @self.bot.message_handler(commands=['entry'])
        async def record_entry(self, message):
            await self.bot.send_message(message.chat.id, 
                "Введите данные входа в формате:\n"
                "ID_сделки цена объем\n"
                "Пример: 1 285.50 10")
        
        # Команда для записи выхода
        @self.bot.message_handler(commands=['exit'])
        async def record_exit(self, message):
            await self.bot.send_message(message.chat.id,
                "Введите данные выхода в формате:\n"
                "ID_сделки цена причина\n"
                "Пример: 1 290.20 take_profit\n"
                "Причины: stop_loss, take_profit, manual, emotion")
        
        # Команда для психологической заметки
        @self.bot.message_handler(commands=['emotion'])
        async def record_emotion(self, message):
            await self.bot.send_message(message.chat.id,
                "Опишите ваше состояние:\n"
                "ID_сделки эмоция уверенность(1-10) заметка\n"
                "Пример: 1 confident 8 Уверен в пробое")


class EnhancedMoexScanner(MoexScanner):
    def __init__(self, telegram_token: str, chat_id: str):
        super().__init__(telegram_token, chat_id)
        self.trade_journal = TradeJournal()
        self.observation_mode = True
        self.last_signal_id = None
        self.user_states = {}  # Для отслеживания состояния пользователей
        self.setup_simple_commands()
    
    def setup_simple_commands(self):
        """Простая настройка - команды обрабатываются в основном цикле"""
        self.commands = {
            '/start': self.cmd_start,
            '/observe': self.cmd_observe,
            '/export': self.cmd_export,
            '/record': self.cmd_record,
            '/note': self.cmd_note,
            '/help': self.cmd_help
        }
    
    async def cmd_start(self, chat_id):
        """Команда /start"""
        await self.bot.send_message(
            chat_id=chat_id,
            text="🤖 **Бот сканера акций запущен!**\n\n"
                 "Доступные команды:\n"
                 "/observe - режим наблюдения\n"
                 "/record - записать решение\n" 
                 "/note ID эмоция текст - добавить заметку\n"
                 "/export - выгрузить отчет\n"
                 "/help - помощь"
        )
    
    async def cmd_observe(self, chat_id):
        """Команда /observe"""
        self.observation_mode = True
        await self.bot.send_message(
            chat_id=chat_id,
            text="🔍 **РЕЖИМ НАБЛЮДЕНИЯ АКТИВИРОВАН**\n\n"
                 "Все сигналы будут записываться в журнал без реального исполнения."
        )
    
    async def cmd_export(self, chat_id):
        """Команда /export"""
        report = await self.export_observations()
        # Если отчет слишком длинный, разбиваем на части
        if len(report) > 4000:
            parts = [report[i:i+4000] for i in range(0, len(report), 4000)]
            for part in parts:
                await self.bot.send_message(chat_id=chat_id, text=part)
        else:
            await self.bot.send_message(chat_id=chat_id, text=report)
    
    async def cmd_record(self, chat_id):
        """Команда /record"""
        if not self.last_signal_id:
            await self.bot.send_message(
                chat_id=chat_id,
                text="❌ Нет активных сигналов для принятия решения"
            )
            return
        
        keyboard = [
            [InlineKeyboardButton("✅ ВОШЕЛ БЫ", callback_data=f"enter_{self.last_signal_id}")],
            [InlineKeyboardButton("❌ НЕ ВОШЕЛ БЫ", callback_data=f"not_enter_{self.last_signal_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await self.bot.send_message(
            chat_id=chat_id,
            text="Выберите решение по последнему сигналу:",
            reply_markup=reply_markup
        )
    
    async def cmd_note(self, chat_id, args):
        """Команда /note ID эмоция текст"""
        if len(args) < 2:
            await self.bot.send_message(
                chat_id=chat_id,
                text="❌ Формат: /note ID эмоция [текст]\nПример: /note 1 confident Четкий пробой"
            )
            return
        
        try:
            trade_id = int(args[0])
            emotion = args[1]
            note_text = ' '.join(args[2:]) if len(args) > 2 else ""
            
            self.trade_journal.add_psychological_note(trade_id, emotion, 5, note_text)
            await self.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Заметка добавлена к наблюдению #{trade_id}"
            )
        except ValueError:
            await self.bot.send_message(
                chat_id=chat_id,
                text="❌ Ошибка: ID должен быть числом"
            )
    
    async def cmd_help(self, chat_id):
        """Команда /help"""
        help_text = """
📋 **ДОСТУПНЫЕ КОМАНДЫ:**

/start - запуск бота
/observe - режим наблюдения
/record - записать решение по последнему сигналу
/note ID эмоция текст - добавить заметку
/export - выгрузить отчет наблюдений
/help - эта справка

📝 **БЫСТРЫЕ КОМАНДЫ ТЕКСТОМ:**
ДА ID уверенность причина
НЕТ ID уверенность причина  
НЕУВЕРЕН ID уверенность причина

Пример: "ДА 1 8 Четкий пробой уровня"
        """
        await self.bot.send_message(chat_id=chat_id, text=help_text)
    
    async def process_message(self, message_text, chat_id):
        """Основной метод обработки входящих сообщений"""
        text = message_text.strip()
        
        # Обработка команд
        if text.startswith('/'):
            parts = text.split()
            command = parts[0].lower()
            
            if command in self.commands:
                if command == '/note' and len(parts) >= 3:
                    await self.commands[command](chat_id, parts[1:])
                else:
                    await self.commands[command](chat_id)
            else:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text="❌ Неизвестная команда. Используйте /help для списка команд."
                )
            return
        
        # Обработка быстрых решений
        decision_keywords = ['ДА ', 'НЕТ ', 'НЕУВЕРЕН ']
        if any(text.startswith(keyword) for keyword in decision_keywords):
            await self.process_quick_decision(text, chat_id)
            return
        
        # Обработка произвольного текста
        await self.bot.send_message(
            chat_id=chat_id,
            text="ℹ️ Для работы используйте команды. /help - для справки."
        )
    
    async def process_quick_decision(self, text, chat_id):
        """Обработка быстрых текстовых решений"""
        parts = text.split(' ', 3)
        decision_map = {
            'ДА': 'would_enter',
            'НЕТ': 'would_not_enter', 
            'НЕУВЕРЕН': 'unsure'
        }
        
        decision = decision_map.get(parts[0])
        if decision and len(parts) >= 3:
            try:
                trade_id = int(parts[1])
                confidence = int(parts[2])
                reasoning = parts[3] if len(parts) > 3 else "Решение через текст"
                
                self.trade_journal.record_final_decision(
                    trade_id, decision, confidence, reasoning
                )
                
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ Решение записано для наблюдения #{trade_id}"
                )
            except ValueError:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text="❌ Ошибка: ID и уверенность должны быть числами"
                )
        else:
            await self.bot.send_message(
                chat_id=chat_id,
                text="❌ Формат: ДА/НЕТ/НЕУВЕРЕН ID уверенность [причина]"
            )
    
    async def handle_callback(self, update):
        """Обработка callback от кнопок (упрощенная)"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        chat_id = query.message.chat_id
        
        if data.startswith('enter_'):
            trade_id = int(data.split('_')[1])
            self.trade_journal.record_final_decision(
                trade_id, 'would_enter', 8, "Решил через кнопку"
            )
            await self.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Решение 'ВОШЕЛ БЫ' записано для наблюдения #{trade_id}"
            )
        elif data.startswith('not_enter_'):
            trade_id = int(data.split('_')[2])
            self.trade_journal.record_final_decision(
                trade_id, 'would_not_enter', 8, "Решил через кнопку"
            )
            await self.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Решение 'НЕ ВОШЕЛ БЫ' записано для наблюдения #{trade_id}"
            )
    
    async def process_signal_for_observation(self, signal):
        """Обработка сигналов в режиме наблюдения"""
        trade_id = self.trade_journal.record_trade_signal(signal)
        self.last_signal_id = trade_id
        await self.send_observation_alert(signal, trade_id)
        return trade_id
    
    async def send_observation_alert(self, signal, trade_id):
        """Отправка сигнала для наблюдения"""
        emoji = "🔍"
        
        keyboard = [
            [InlineKeyboardButton("✅ ВОШЕЛ БЫ", callback_data=f"enter_{trade_id}")],
            [InlineKeyboardButton("❌ НЕ ВОШЕЛ БЫ", callback_data=f"not_enter_{trade_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = f"""
{emoji} **СИГНАЛ ДЛЯ НАБЛЮДЕНИЯ** #{trade_id}

🎯 **Акция**: {signal['ticker']}
📊 **Таймфрейм**: {signal['timeframe']}
⚡ **Сигнал**: {signal['signal']}

💰 **Цена**: {signal['current_price']:.2f} ₽
🛑 **Стоп-Лосс**: {signal['stop_loss']:.2f} ₽
🎯 **Тейк-Профит**: {signal['take_profit']:.2f} ₽

⏰ **Время**: {datetime.now().strftime('%H:%M %d.%m.%Y')}

**Для записи решения:**
• Используйте кнопки ниже
• Или отправьте: ДА/НЕТ {trade_id} уверенность причина
• Или команду: /record

/help - список всех команд
"""
        
        await self.bot.send_message(
            chat_id=self.chat_id,
            text=message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def export_observations(self):
        """Экспорт данных наблюдений"""
        try:
            observations = [t for t in self.trade_journal.trades 
                          if t.get('status') == 'OBSERVATION']
            
            if not observations:
                return "📊 Нет данных наблюдений для экспорта"
            
            report = f"📊 ОТЧЕТ ПО НАБЛЮДЕНИЯМ\n"
            report += f"Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
            report += f"Всего сигналов: {len(observations)}\n\n"
            
            for obs in observations:
                signal = obs['signal_data']
                report += f"#{obs['trade_id']} {signal['ticker']} {signal['signal']}\n"
                report += f"  Цена: {signal['current_price']}₽ | SL: {signal['stop_loss']}₽ | TP: {signal['take_profit']}₽\n"
                
                if obs.get('final_decision'):
                    decision = obs['final_decision']
                    report += f"  Решение: {decision['decision']} "
                    report += f"(уверенность: {decision['confidence']}/10)\n"
                    report += f"  Причина: {decision['reasoning']}\n"
                else:
                    report += "  Решение: НЕ ПРИНЯТО\n"
                
                report += "\n"
            
            return report
            
        except Exception as e:
            return f"❌ Ошибка при экспорте: {str(e)}"

async def message_polling(scanner):
    """Простой поллинг сообщений"""
    offset = None
    
    while True:
        try:
            updates = await scanner.bot.get_updates(offset=offset, timeout=30)
            
            for update in updates:
                offset = update.update_id + 1
                
                if update.message:
                    # Обработка текстовых сообщений
                    await scanner.process_message(
                        update.message.text, 
                        update.message.chat_id
                    )
                elif update.callback_query:
                    # Обработка callback от кнопок
                    await scanner.handle_callback(update)
                    
        except Exception as e:
            print(f"Ошибка в поллинге: {e}")
            await asyncio.sleep(5)

async def run_continuous_observation(self):
    """Непрерывное сканирование в режиме наблюдения"""
    while True:
        try:
            if self.observation_mode:
                await self.scan_market()
            await asyncio.sleep(300)  # Сканируем каждые 5 минут
        except Exception as e:
            print(f"Ошибка сканирования: {e}")
            await asyncio.sleep(60)

EnhancedMoexScanner.run_continuous_observation = run_continuous_observation

# Конфигурация и запуск
async def main():
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    
    # Создаем и запускаем сканер
    scanner = EnhancedMoexScanner(
        telegram_token=TELEGRAM_BOT_TOKEN,
        chat_id=TELEGRAM_CHAT_ID
    )
    
    # Запускаем сканер
    await asyncio.gather(
        message_polling(scanner),
        scanner.run_continuous_observation()
    )

if __name__ == "__main__":
    # Для запуска:
    asyncio.run(main())