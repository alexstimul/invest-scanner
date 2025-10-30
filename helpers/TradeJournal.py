class TradeJournal:
    def __init__(self):
        self.journal_file = "trade_journal.json"
        self.trades = []
        self.load_existing_journal()
    
    def load_existing_journal(self):
        try:
            with open(self.journal_file, 'r', encoding='utf-8') as f:
                self.trades = json.load(f)
        except FileNotFoundError:
            self.trades = []
    
    def record_trade_signal(self, signal_data):
        """Запись сигнала для наблюдения"""
        trade_id = len(self.trades) + 1
        
        trade_record = {
            'trade_id': trade_id,
            'signal_time': datetime.now().isoformat(),
            'signal_data': signal_data,
            'status': 'OBSERVATION',
            'psychological_notes': [],
            'final_decision': None
        }
        
        self.trades.append(trade_record)
        self.save_journal()
        return trade_id
    
    def add_psychological_note(self, trade_id, emotion, confidence, note):
        """Добавление психологической заметки"""
        for trade in self.trades:
            if trade['trade_id'] == trade_id:
                trade['psychological_notes'].append({
                    'timestamp': datetime.now().isoformat(),
                    'emotion': emotion,
                    'confidence_level': confidence,
                    'note': note
                })
                break
        self.save_journal()
    
    def record_final_decision(self, trade_id, decision, confidence, reasoning):
        """Запись итогового решения"""
        for trade in self.trades:
            if trade['trade_id'] == trade_id:
                trade['final_decision'] = {
                    'decision': decision,
                    'confidence': confidence,
                    'reasoning': reasoning,
                    'timestamp': datetime.now().isoformat()
                }
                break
        self.save_journal()
    
    def save_journal(self):
        """Сохранение журнала"""
        with open(self.journal_file, 'w', encoding='utf-8') as f:
            json.dump(self.trades, f, indent=2, ensure_ascii=False)