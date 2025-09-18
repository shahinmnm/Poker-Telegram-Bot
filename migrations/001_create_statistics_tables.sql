-- Schema for the poker bot statistics subsystem.
-- Designed for PostgreSQL (recommended) but compatible with MySQL 8+.
-- Execute this migration before enabling the statistics service.

CREATE TABLE IF NOT EXISTS player_stats (
    user_id BIGINT PRIMARY KEY,
    display_name VARCHAR(255),
    username VARCHAR(255),
    first_seen TIMESTAMP NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMP NOT NULL DEFAULT NOW(),
    last_game_at TIMESTAMP,
    last_bonus_at TIMESTAMP,
    last_private_chat_id BIGINT,
    total_games INTEGER NOT NULL DEFAULT 0,
    total_wins INTEGER NOT NULL DEFAULT 0,
    total_losses INTEGER NOT NULL DEFAULT 0,
    total_play_time BIGINT NOT NULL DEFAULT 0,
    total_amount_won BIGINT NOT NULL DEFAULT 0,
    total_amount_lost BIGINT NOT NULL DEFAULT 0,
    biggest_win_amount BIGINT NOT NULL DEFAULT 0,
    biggest_win_hand VARCHAR(128),
    current_win_streak INTEGER NOT NULL DEFAULT 0,
    current_loss_streak INTEGER NOT NULL DEFAULT 0,
    longest_win_streak INTEGER NOT NULL DEFAULT 0,
    longest_loss_streak INTEGER NOT NULL DEFAULT 0,
    most_common_winning_hand VARCHAR(128),
    most_common_winning_hand_count INTEGER NOT NULL DEFAULT 0,
    lifetime_bet_amount BIGINT NOT NULL DEFAULT 0,
    lifetime_profit BIGINT NOT NULL DEFAULT 0,
    total_all_in_wins INTEGER NOT NULL DEFAULT 0,
    total_all_in_events INTEGER NOT NULL DEFAULT 0,
    total_showdowns INTEGER NOT NULL DEFAULT 0,
    total_pot_participated BIGINT NOT NULL DEFAULT 0,
    largest_pot_participated BIGINT NOT NULL DEFAULT 0,
    total_bonus_claimed BIGINT NOT NULL DEFAULT 0,
    last_result VARCHAR(16)
);

CREATE TABLE IF NOT EXISTS game_sessions (
    hand_id VARCHAR(64) PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMP,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    pot_total BIGINT NOT NULL DEFAULT 0,
    participant_count INTEGER NOT NULL DEFAULT 0,
    top_winning_hand VARCHAR(128),
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS game_participants (
    id SERIAL PRIMARY KEY,
    hand_id VARCHAR(64) NOT NULL,
    user_id BIGINT NOT NULL,
    joined_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_game_participant UNIQUE (hand_id, user_id)
);

CREATE INDEX IF NOT EXISTS ix_game_participants_hand_id ON game_participants(hand_id);
CREATE INDEX IF NOT EXISTS ix_game_participants_user_id ON game_participants(user_id);

CREATE TABLE IF NOT EXISTS player_hand_history (
    id SERIAL PRIMARY KEY,
    hand_id VARCHAR(64) NOT NULL,
    user_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    hand_type VARCHAR(128),
    result VARCHAR(16) NOT NULL,
    amount_won BIGINT NOT NULL DEFAULT 0,
    amount_lost BIGINT NOT NULL DEFAULT 0,
    net_profit BIGINT NOT NULL DEFAULT 0,
    total_bet BIGINT NOT NULL DEFAULT 0,
    pot_size BIGINT NOT NULL DEFAULT 0,
    was_all_in BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS ix_player_hand_history_hand_id ON player_hand_history(hand_id);
CREATE INDEX IF NOT EXISTS ix_player_hand_history_user_id ON player_hand_history(user_id);

CREATE TABLE IF NOT EXISTS player_winning_hands (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    hand_type VARCHAR(128) NOT NULL,
    win_count INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT uq_player_winning_hand UNIQUE (user_id, hand_type)
);

