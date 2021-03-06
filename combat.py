# -*- coding: utf-8 -*-
"""
combat.py

Description:
    Play games between 2 chess engines.

Tested on:
    python 3.7.4
    python-chess v0.30.1
    windows 10    
"""


import os
import sys
import concurrent.futures
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path  # Python 3.4
import time
from datetime import datetime
import random
import configparser
import collections
import argparse
import json
import chess.pgn
import chess.engine
import logging


APP_NAME = 'combat'
APP_VERSION = 'v1.28'


# Increase limit to fix RecursionError
sys.setrecursionlimit(10000)  


# Save created loggers to be used later when creating other loggers, making
# sure that logger has no duplicates to avoid double entries in the logs.
saved_loggers = {}
        

class Timer():
    def __init__(self, btms, itms):
        """ 
        btms: base time in ms
        itms: inc time in ms
        """
        self.btms = btms
        self.itms = itms
        self.rem_time = btms
        self.tf = False  # tf is time forfeit
        
    def update_time(self, elapse, log_fn):
        """ 
        elapse: time in ms spent on a search
        """  
        logger = setup_logging('update_time', log_fn)
        if self.rem_time - int(elapse) < 1:
            logger.warning('Remaining time is below 1ms before adding the increment!')
            self.tf = True
        
        self.rem_time += self.itms - int(elapse)


class Match():    
    def __init__(self, start_game, player1, player2,
                 round_num, total_games, game_id, log_fn, adjudication=False,
                 win_score_cp=700, win_score_count=4, is_engine_log=False):
        self.start_game = start_game
        self.eng_files = [player1['file'], player2['file']]
        self.eng_opts = [player1['opt'], player2['opt']]
        self.eng_names = [player1['name'], player2['name']]
        self.clock = [player1['clock'], player2['clock']]
        self.round_num = round_num  # for pgn header
        self.total_games = total_games
        self.time_forfeit = [False, False]
        self.write_time_forfeit_result = True
        self.game_id = game_id
        self.log_fn = log_fn
        self.adjudication = adjudication
        self.win_score_cp = win_score_cp
        self.win_score_count = win_score_count
        self.is_engine_log = is_engine_log

    def update_headers(self, game, board, wplayer, bplayer, score_adjudication, elapse):
        ga = chess.pgn.Game()
        g = ga.from_board(board)
        
        game.headers['Event'] = 'Computer games'
        game.headers['Site'] = 'Combat'
        game.headers['Date'] = datetime.today().strftime('%Y.%m.%d')
        
        try:
            game.headers['FEN'] = g.headers['FEN']    
        except:
            pass
        
        if self.time_forfeit[1] or self.time_forfeit[0]:
            game.headers['Termination'] = 'time forfeit'
            
            if self.write_time_forfeit_result:
                if self.time_forfeit[1]:
                    game.headers['Result'] = '0-1'
                else:
                    game.headers['Result'] = '1-0'
            else:
                game.headers['Result'] = '*'
                game.headers['Termination'] = 'unterminated'
        
        # Win score adjudication
        elif score_adjudication[0] or score_adjudication[1]:
            adj_value = 'adjudication: good score for white' if score_adjudication[1] else 'adjudication: good score for black'
            game.headers['Termination'] = adj_value
            
            game.headers['Result'] = '1-0' if score_adjudication[1] else '0-1'            
        
        else:
            game.headers['Result'] = g.headers['Result']
            
            if board.is_checkmate():
                game.headers['Termination'] = 'checkmate'
                
            elif board.is_stalemate():
                game.headers['Termination'] = 'stalemate'
                
            elif board.is_insufficient_material():
                game.headers['Termination'] = 'insufficient mating material'
                
            elif board.can_claim_fifty_moves():
                game.headers['Termination'] = 'fifty-move draw rule' 
                
            elif board.is_repetition(count=3):
                game.headers['Termination'] = 'threefold repetition'
                
            else:
                # Todo: Check the game if this is triggered, not encountered so far.
                game.headers['Termination'] = 'unknown'
        
        game.headers['Round'] = self.round_num
        game.headers['White'] = wplayer
        game.headers['Black'] = bplayer
        
        game.headers['WhiteTimeControl'] = \
            f'{get_time_h_mm_ss_ms(self.clock[1].btms*1000000)} + {get_time_h_mm_ss_ms(self.clock[1].itms*1000000, True)}'
        game.headers['BlackTimeControl'] = \
            f'{get_time_h_mm_ss_ms(self.clock[0].btms*1000000)} + {get_time_h_mm_ss_ms(self.clock[0].itms*1000000, True)}'
            
        game.headers['GameDuration'] = get_time_h_mm_ss_ms(elapse)

        return game
    
    def get_search_info(self, result, info):
        logger = setup_logging('search_info', self.log_fn)
        if info == 'score':
            score = None
            try:
                score = result.info[info].relative.score(mate_score=32000)
            except KeyError as e:
                logger.warning(e)
            except Exception:
                logger.exception(f'Exception in getting {info} from search info.')
                logger.debug(result)
                
            return score
        
        elif info == 'depth':
            depth = None
            try:
                depth = result.info[info]
            except KeyError as e:
                logger.warning(e)
            except Exception:
                logger.exception(f'Exception in getting {info} from search info.')
                logger.exception(result)
                
            return depth
        
        elif info == 'time':
            time = None
            try:
                time = result.info[info] * 1000
            except KeyError as e:
                logger.warning(e)
            except Exception:
                logger.exception(f'Exception in getting {info} from search info.')
                logger.debug(result)
                
            return time
        
        elif info == 'nodes':
            nodes = None
            try:
                nodes = result.info[info]
            except KeyError as e:
                logger.warning(e)
            except Exception:
                logger.exception(f'Exception in getting {info} from search info.')
                logger.debug(result)
                
            return nodes
        
        return None
    
    def win_score_adjudication(self, wscores, bscores):
        """
        Adjudicate game by score, scores of one side should be successively
        winning and the scores of other side are successively losing.
        
        wscores: a list of white scores
        bscores: a list of black scores
        
        Return: 
            [True, False] if game is good for black
            [False, True] if game is good for white
            [False, False] if game is not to be adjudicated
        """
        logger = setup_logging('adjudication', self.log_fn)
        
        n = self.win_score_count  # Default 4
        w = self.win_score_cp  # Default 700
        ret = [False, False]  # [Black, white]
        
        if len(wscores) < n or len(bscores) < n:
            return ret
        
        # (1) White wins
        w_good_cnt, b_bad_cnt = 0, 0
        for s in wscores[-n:]:  # Check the last n scores
            if s >= w:
                w_good_cnt += 1
                
        for s in bscores[-n:]:
            if s <= -w:
                b_bad_cnt += 1
                
        if w_good_cnt >= n and b_bad_cnt >= n:
            logger.debug(f'White wins by adjudication. White last {n} scores: {wscores[-n:]}, Black last {n} scores: {bscores[-n:]}')
            return [False, True]
        
        # (2) Black wins
        w_bad_cnt, b_good_cnt = 0, 0
        for s in bscores[-n:]:
            if s >= w:
                b_good_cnt += 1
                
        for s in wscores[-n:]:
            if s <= -w:
                w_bad_cnt += 1
                
        if b_good_cnt >= n and w_bad_cnt >= n:
            logger.debug(f'Black wins by adjudication. Black last {n} scores: {bscores[-n:]}, White last {n} scores: {wscores[-n:]}')
            return [True, False]
        
        return ret
    
    def start_match(self):
        logger = setup_logging('Match.start_match', self.log_fn)
        
        # Enable python-chess module engine logger, saved in a different file.
        if self.is_engine_log:
            setup_logging('chess.engine', 'engine_log.txt')
        
        # Save score info per move from both engines for score adjudications
        eng_score = {0: [], 1: []}
        score_adjudication = [False, False]
        
        eng = [chess.engine.SimpleEngine.popen_uci(self.eng_files[0]),
                chess.engine.SimpleEngine.popen_uci(self.eng_files[1])]
        
        # Set options
        for k, v in self.eng_opts[0].items():
            eng[0].configure({k: v})
        for k, v in self.eng_opts[1].items():
            eng[1].configure({k: v})           
        
        # Create a board which will be played by engines.        
        end_node = self.start_game.end()
        end_board = end_node.board()
        board = end_board.copy()
        logger.debug(f'Create board from fen: {board.fen()}')
        
        # Create a game for annotated game output.
        game = chess.pgn.Game()
        game = game.from_board(end_board)
        node = game.end()

        logger.info(f'Starting, game: {self.game_id} / {self.total_games}, round: {self.round_num}, players: {self.eng_names[1]} vs {self.eng_names[0]}')
        
        # First engine with index 0 will handle the black side.
        self.clock[1].rem_time = self.clock[1].btms
        self.clock[0].rem_time = self.clock[0].btms
        
        game_start = time.perf_counter_ns()
        
        # Play the game, till its over by python-chess
        while not board.is_game_over():
            # Get init time in case the engine does not send its time info.
            t1 = time.perf_counter_ns()
            
            # Let engine search for the best move of the given board.
            result = eng[board.turn].play(board, chess.engine.Limit(
                white_clock=self.clock[1].rem_time/1000,
                black_clock=self.clock[0].rem_time/1000,
                white_inc=self.clock[1].itms/1000,
                black_inc=self.clock[0].itms/1000),
                info=chess.engine.INFO_SCORE)
            
            # Get score, depth and time for move comments in pgn output.
            score_cp = self.get_search_info(result, 'score')
            depth = self.get_search_info(result, 'depth')
            time_ms = self.get_search_info(result, 'time')
            
            # Save score for game adjudication based on engine score
            eng_score[board.turn].append(0 if score_cp is None else score_cp)
            
            # If engine does not give time spent, calculate elapse time manually.
            if time_ms is None:
                time_ms = (time.perf_counter_ns() - t1)//1000//1000  # from nano to ms
            time_ms = max(1, time_ms)  # If engine sent time below 1, use a minimum of 1ms
                
            # Update time and determine if engine exceeds allocated time.
            self.clock[board.turn].update_time(time_ms, self.log_fn)
            self.time_forfeit[board.turn] = self.clock[board.turn].tf
            
            # Save move and comment in pgn output file.               
            node = node.add_variation(result.move)
            if score_cp is not None and depth is not None and time_ms is not None:
                node.comment = f'{score_cp/100:+0.2f}/{depth} {time_ms:0.0f}ms'

            # Stop the game if time limit is exceeded.
            if self.clock[board.turn].tf:
                logger.info(f'round: {self.round_num}, infraction: {"white" if board.turn else "black"} loses on time!')
                break
            
            # Update the board with the move for next player
            board.push(result.move) 
            
            # Stop game by score adjudication
            if self.adjudication:
                score_adjudication = self.win_score_adjudication(
                    eng_score[chess.WHITE], eng_score[chess.BLACK])
            
            if score_adjudication[0] or score_adjudication[1]:
                break                    
        
        eng[0].quit()
        eng[1].quit()
        
        elapse = time.perf_counter_ns() - game_start        
        game = self.update_headers(game, board, self.eng_names[1],
                                   self.eng_names[0], score_adjudication,
                                   elapse)
        
        return [game, self.game_id, self.round_num, elapse]
    
    
def setup_logging(name, log_fn='combat_log.txt'):
    """
    Creates logger by name, all logs will be written to log file
    combat_log.txt, depending on the logging level.
    
    At the current setting below the following will be followed:
    * Logging debug levels will be written only to log file.
    * Logging info levels and above will be written to console and log file.
    """
    global saved_loggers
    
    # Use logging.WARNING to disable most logging into the log file.
    # Todo: Make this available via command line options.
    combat_file_log_level = logging.DEBUG
    
    # Do not create the same logger, to avoid double log entries    
    if saved_loggers.get(name):
        return saved_loggers.get(name)
    
    logger = logging.getLogger(name)    
    logger.setLevel(logging.DEBUG)
    
    # Create file handler to write logs to a file
    fh = logging.FileHandler(filename=log_fn, mode='a')
    
    if name == 'chess.engine':
        fh.setLevel(logging.DEBUG)
    else:
        fh.setLevel(combat_file_log_level)
    
    # Create console handler for console logging
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    
    # Create formatter and add it to the handlers
    fh_formatter = logging.Formatter('%(asctime)s - %(name)17s - %(levelname)8s - %(message)s')
    ch_formatter = logging.Formatter('%(message)s')
    
    # Set formats for each handler
    fh.setFormatter(fh_formatter)
    ch.setFormatter(ch_formatter)
    
    # Add handlers to logger
    logger.addHandler(ch)
    logger.addHandler(fh)
    
    saved_loggers[name] = logger
    
    return logger


def get_time_h_mm_ss_ms(time_ns, mmssms = False):
        """
        Converts time delta to hh:mm:ss:ms format.
        
        time_ns: time delta in nanoseconds
        return: time in h:m:s:ms format
        """
        time_ms = time_ns//1000000
        s, ms = divmod(time_ms, 1000)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)

        if mmssms:
            return '{:02d}m:{:02d}s:{:03d}ms'.format(m, s, ms)
        return '{:01d}h:{:02d}m:{:02d}s:{:03d}ms'.format(h, m, s, ms)
    
    
def print_result_table(pd, num_res, log_fn):
    logger = setup_logging('result_table', log_fn)
    
    logger.info('')
    
    tname = [pd[i]['name'] for i in range(len(pd))]
    
    # Result table header
        
    # Get max length of engine names for table formatting.
    width = len(max(tname, key=len))
    
    tn = '{:>{width}}'.format('name', width=width)
    thead = '{} {:>9s} {:>9s} {:>6s} {:>6s} {:>6s} {:>4s}'.format(
        tn, 'score', 'games', 'score%', 'win%', 'draw%', 'tf')
    logger.info(thead)

    # Table data
    for i in range(len(tname)):
        w = pd[i]['win']  # num_win
        l = pd[i]['loss'] # num_loss
        d = pd[i]['draw'] # num_draw
        g = w + l + d
        s = w + d/2
        sr = 100*s/g if g > 0 else 0.0
        wr = 100*w/g if g > 0 else 0.0
        dr = 100*d/g if g > 0 else 0.0
        tf = pd[i]['tf']
        
        tn = '{:>{width}}'.format(tname[i], width=width)
        
        logger.info('{} {:>9.1f} {:>9d} {:>6.1f} {:>6.1f} {:>6.1f} {:>4d}'.format(
            tn,
            s,
            g,
            sr,
            wr,
            dr,
            tf))
        
    logger.info('')
    

def update_score(g, pd):
    """ 
    pd: {0: {'name': 'engname', 'engfile': 'a.exe', 'engopt': oa ...}, 1: {...} ...}
    """
    res = g.headers['Result']
    wp = g.headers['White']
    bp = g.headers['Black']
    termi = g.headers['Termination']
    
    if res == '1-0':
        for i in range(len(pd)):
            if wp == pd[i]['name']:
                pd[i]['win'] += 1
            if bp == pd[i]['name']:
                pd[i]['loss'] += 1
                if termi == 'time forfeit':
                    pd[i]['tf'] += 1
                
    elif res == '0-1':
        for i in range(len(pd)):
            if bp == pd[i]['name']:
                pd[i]['win'] += 1
            if wp == pd[i]['name']:
                pd[i]['loss'] += 1
                if termi == 'time forfeit':
                    pd[i]['tf'] += 1                
                
    elif res == '1/2-1/2':
        for i in range(len(pd)):
            if wp == pd[i]['name']:
                pd[i]['draw'] += 1
            if bp == pd[i]['name']:
                pd[i]['draw'] += 1
        
    return pd


def get_game_list(fn, log_fn, max_round=500, randomize_pos=False):
    """ 
    Converts fn file into python-chess game objects.
    
    fn: can be a fen or an epd or a pgn file
    Return: a list of games
    """
    logger = setup_logging(get_game_list.__name__, log_fn)
    
    t1 = time.perf_counter_ns()
    
    games = []
    
    fn_filename = Path(fn).name
    file_suffix = Path(fn).suffix
    
    logger.info(f'Preparing start opening from {fn_filename} ...')
    
    if file_suffix not in ['.pgn', '.fen', '.epd']:
        raise  Exception(f'File {fn} has no extension!, accepted file ext: .pgn, .fen, .epd')
    
    if file_suffix == '.pgn':
        with open(fn) as pgn:
            while True:
                game = chess.pgn.read_game(pgn)
                if game is None:
                    break
                games.append(game)
                if len(games) >= max_round:
                    break
    else:
        with open(fn) as pos:
            for lines in pos:
                line = lines.strip()
                board = chess.Board(line)
                game = chess.pgn.Game()
                game = game.from_board(board)
                games.append(game)
                if len(games) >= max_round:
                    break
                
    if randomize_pos:
        random.shuffle(games)
        
    elapse = time.perf_counter_ns() - t1
    
    if len(games) < max_round:
        logger.info(f'Number of positions in the file {len(games)} are below max_round {max_round}!')
    logger.info(f'status: done, games prepared: {len(games)}, elapse: {get_time_h_mm_ss_ms(elapse)}\n')
        
    return games


def print_match_conditions(max_round, reverse_start_side, opening_file,
                           randomize_pos, parallel, base_time_ms, inc_time_ms,
                           adjudication, win_score_cp, win_score_count, log_fn):
    logger = setup_logging('match_conditions', log_fn)
    
    logger.info(f'rounds           : {max_round}')
    logger.info(f'reverse side     : {reverse_start_side}')
    logger.info(f'total games      : {max_round*2 if reverse_start_side else max_round}')
    logger.info(f'opening file     : {opening_file}')
    logger.info(f'randomize fen    : {randomize_pos}')        
    logger.info(f'base time(ms)    : {base_time_ms}')
    logger.info(f'inc time(ms)     : {inc_time_ms}')    
    logger.info(f'win adjudication : {adjudication}')
    logger.info(f'win score cp     : {win_score_cp}')
    logger.info(f'win score count  : {win_score_count}')
    logger.info(f'parallel         : {parallel}\n')
    
    
def get_engine_data(fn, ename, log_fn):
    """
    Read engine json file to get options etc. of ename.
    
    fn: engine json file
    ename: engine config name to search
    return: eng path and file and its options that are not default
    """
    logger = setup_logging('engine_data', log_fn)
    path_file = None
    opt = {}
    
    with open(fn) as json_file:
        data = json.load(json_file)
        
    for p in data:
        command = p['command']
        work_dir = p['workingDirectory']
        name = p['name']
        
        if name != ename:
            continue
        
        path_file = Path(work_dir, command).as_posix()
        
        for k, v in p.items():
            if k == 'options':
                for o in v:
                    # d = {'name': 'Ponder', 'default': False, 'value': False, 'type': 'check'}
                    opt_name = o['name']
                    
                    try:
                        opt_default = o['default']
                    except KeyError:
                        continue
                    except Exception:
                        logger.exception('Error in getting default option value!')
                        continue
                        
                    opt_value = o['value']
                    if opt_default != opt_value:
                        opt.update({opt_name: opt_value})
        
        return path_file, opt
    

def get_match_data(engine_option_value, match_fn, rounds, reverse, parallel,
                       opt_win_adjudication, win_adj, win_score_cp,
                       win_score_count, engine_json, op_file, random_pos,
                       is_engine_log):
    """
    Get command line option values or match.ini file and others.
    """
    
    players, names, base_time_ms, inc_time_ms = {}, [], None, None
    
    if engine_option_value:         
        for i, e in enumerate(engine_option_value):
            name, base_time_ms, inc_time_ms = None, None, None
            for v in e:
                par_name = v.split('=')[0]
                par_value = v.split('=')[1]
                if par_name == 'config-name':
                    name = par_value
                elif par_name == 'tc':
                    tc_val = par_value
                    base_time_ms = int(tc_val.split('+')[0])
                    try:
                        inc_time_ms = int(tc_val.split('+')[1])
                    except IndexError:
                        raise Exception('Time increment is missing!')
                    
            names.append(name)
            d = {i: {'name': name, 'base': base_time_ms, 'inc': inc_time_ms}}
            players.update(d)
            
        players = collections.OrderedDict(sorted(players.items()))
        
        # --win-adjudication score=700 count=4
        if opt_win_adjudication:
            win_adj = True
            for v in opt_win_adjudication:
                value = v.split('=')
                if value[0] == 'score':
                    win_score_cp = int(value[1])
                elif value[0] == 'count':
                    win_score_count = int(value[1])
                
    else:
        # Read match.ini to determine the player names, etc
        parser = configparser.ConfigParser()
        parser.read(match_fn)
        for section_name in parser.sections():
            for name, value in parser.items(section_name):
                if section_name.lower() == 'combat':
                    if name == 'engine config file':
                        engine_json = value
                    elif name == 'round':
                        rounds = int(value)
                    elif name == 'opening file':
                        op_file = value
                    elif name == 'reverse':
                        reverse = value
                    elif name == 'randomize position':
                        random_pos = value
                    elif name == 'parallel':
                        parallel = int(value)                        
                    elif name == 'win adjudication enable':
                        win_adj = value
                    elif name == 'win adjudication score':
                        win_score_cp = int(value)
                    elif name == 'win adjudication count':
                        win_score_count = int(value)
                    elif name == 'engine logging':
                        is_engine_log = True if value == 'true' else False
                        
                elif section_name.lower() == 'engine1':
                    if name == 'name':
                        name = value
                        names.append(name)
                    elif name.lower() == 'tc':
                        base_time_ms = int(value.split('+')[0])
                        inc_time_ms = int(value.split('+')[1])
                        
                    d = {0: {'name': name, 'base': base_time_ms, 'inc': inc_time_ms}}
                    players.update(d)
                    
                elif section_name.lower() == 'engine2':
                    if name.lower() == 'name':
                        name = value
                        names.append(name)
                    elif name.lower() == 'tc':
                        base_time_ms = int(value.split('+')[0])
                        inc_time_ms = int(value.split('+')[1])
                        
                    d = {1: {'name': name, 'base': base_time_ms, 'inc': inc_time_ms}}
                    players.update(d)
    
    return players, base_time_ms, inc_time_ms, names, op_file, random_pos, \
        rounds, reverse, parallel, win_adj, win_score_cp, win_score_count, \
        engine_json, is_engine_log


def delete_file(*fns):
    """
    Delete tuple elements in fns.
    """
    for fn in fns:        
        try:
            os.remove(fn)
        except OSError:
            pass
        

def get_opening_data(opt_value):
    opening_file, randomize_pos = None, False
    for v in opt_value:
        value = v.split('=')
        if value[0] == 'file':
            opening_file = value[1]
        elif value[0] == 'random':
            randomize_pos = True if value[1] == 'true' else False
            
    return opening_file, randomize_pos


def error_check(players, names):
    # Stop the script if one of the engine names is not defined.
    if None in names:
        raise Exception('Engine config name should not be None')

    # Stop the script if engine clock is not defined
    for i in range(len(names)):
        if None in [players[i]['base'], players[i]['inc']]:
            raise Exception(f'{"Black" if i == 0 else "White"} TC was not defined! Use tc=base_time_ms+inc_time_ms')

def get_engine_file_and_option(engine_json, names, log_fn):
    """
    Return engine file and its option in engine json file.
    """    
    eng_files, eng_opts = [None] * len(names), [None] * len(names)
    for i in range(len(names)):
        try:
            eng_files[i], eng_opts[i] = get_engine_data(engine_json, names[i], log_fn)
        except TypeError:
            raise Exception(f'engine {names[i]} cannot be found in {Path(engine_json).name}!')
        except Exception:
            raise Exception(f'Exception occurs in getting engine data from {Path(engine_json).name}')
            
    return eng_files, eng_opts


def get_clock(players):
    """
    Create clock for each engine and return it.
    """    
    clock = []
    for _, v in players.items():
        clock.append(Timer(v['base'], v['inc']))
        
    return clock

    
def main():    
    parser = argparse.ArgumentParser(
        prog='%s' % (APP_NAME),
        description='Run engine vs engine match',
        epilog='%(prog)s' + ' %s' % APP_VERSION,
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--engine-config-file', required=False,
                        default='combat.json',
                        help='This is used to define the file where\n'
                        'engine configurations are located. You may use\n'
                        'included file combat.json or you can use engines.json\n'
                        'from cutechess. example:\n'
                        '--engine-config-file combat.json\n'
                        'or using the engines.json from cutechess\n'
                        '--engine-config-file "d:/cutechess/engines.json"\n'
                        'default is combat.json')
    parser.add_argument('--engine', nargs='*', action='append', required=False,
                        help='This option is used to define the engines\n'
                        'playing in the match. It also include tc or time control.\n'
                        'You need to call this twice or more. See example below.\n'
                        'format:\n'
                        '--engine config-name=E1 tc=btms1+itms1 --engine config-name=E2 tc=btms2+itms2\n'
                        'where:\n'
                        'E1 = engine name from combat.json or engine config file\n'
                        'btms1 = base time in ms for engine 1\n'
                        'itms1 = increment time in ms for engine 1\n'
                        'example:\n'
                        '--engine config-name="Deuterium v2019.2" tc=60000+100 --engine config-name="Deuterium v2019.2 mobility130" tc=60000+100\n'
                        'note:\n'
                        '* engine1 will play as black, in the example above\n'
                        '  this is Deuterium v2019.2\n'
                        '* engine2 will play as white\n'
                        '* When round reverse is true the side is reversed that is\n'
                        '  engine1 will play as white and engine2 will play as black')
    parser.add_argument('--opening', nargs='*', required=False, 
                        default=['file=grand_swiss_2019_6plies.pgn', 'random=False'],
                        help='Opening file is used by engine to start the game.\n'
                        'You may use pgn or epd or fen formats.\n'
                        'example:\n'
                        '--opening file=start.pgn random=true\n'
                        'or with file path\n'
                        '--opening file="d:/chess/opening_start.pgn" random=true\n'
                        'or with epd file\n'
                        '--opening file="d:/chess/opening.epd" random=true\n'
                        'or to not use random\n'
                        '--opening file="d:/chess/opening.epd" random=false\n'
                        'default value is ["file=grand_swiss_2019_6plies.pgn", "random=False"]')
    parser.add_argument('--round', default=500, type=int,
                        help='number of games to play, twice if reverse is true')
    parser.add_argument('--reverse', action='store_true',
                        help='A flag to reverse start side.')
    parser.add_argument('--parallel', default=1, type=int, required=False,
                        help='option to run game matches in parallel, default=1')
    parser.add_argument('--win-adjudication', nargs='*', required=False,
                        help='Option to stop the game when one side is\n'
                        'already ahead on score. Both engines would agree\n'
                        'that one side is winning and the other side is lossing.\n'
                        'example:\n'
                        '--win-adjudication score=700 count=4\n'
                        'where:\n'
                        '  score: engine score in cp\n'
                        '  count: number of times the score is recorded')
    parser.add_argument('--output', default='output_games.pgn',
                        help='Save output games, default=output_games.pgn')
    parser.add_argument('--log-filename', default='combat_log.txt',
                        help='A filename to save its logs. default=combat_log.txt')
    parser.add_argument('--engine-log', action='store_true',
                        help='A flag to save engine log to a file.')
    parser.add_argument('--gauntlet-color',
                        help='Set the color of gauntlet to either white or black. Example --gauntlet-color white')
    parser.add_argument('-v', '--version', action='version',
                        version='%(prog)s ' + APP_VERSION)
    
    args = parser.parse_args()
    outpgn = args.output
    parallel = args.parallel  
    max_round = args.round
    reverse_start_side = args.reverse
    engine_json = args.engine_config_file
    gauntlet_color = args.gauntlet_color
    
    # Init logging
    log_fn = args.log_filename
    engine_log_fn = 'engine_log.txt'
    is_engine_log = args.engine_log
    match_fn = 'match.ini'
    
    # Delete existing log files everytime combat is run.
    delete_file(log_fn, engine_log_fn)
    
    logger = setup_logging(main.__name__, log_fn)
    
    # Get command line option values and/or data from match.ini file
    opening_file, randomize_pos = None, False
    win_adj, win_score_cp, win_score_count = False, 700, 4
    
    opening_file, randomize_pos = get_opening_data(args.opening)
    
    players, base_time_ms, inc_time_ms, names, opening_file, \
        randomize_pos, max_round, reverse_start_side, parallel, \
        win_adj, win_score_cp, win_score_count, engine_json, is_engine_log = \
        get_match_data(
            args.engine, match_fn, max_round, reverse_start_side, parallel,
            args.win_adjudication, win_adj, win_score_cp, win_score_count,
            engine_json, opening_file, randomize_pos, is_engine_log)

    # Create clock per player
    clock = get_clock(players)
    
    # Stop the script at this point if there are errors
    error_check(players, names)

    # Get eng file and options from engine json file
    eng_files, eng_opts = get_engine_file_and_option(engine_json, names, log_fn)
    
    # Save overall player_data in a dict
    player_data = {}
    for i, (n, f, o, c) in enumerate(zip(names, eng_files, eng_opts, clock)):
        d = {i: {'name': n, 'file': f, 'opt': o,
                 'clock': c, 'win': 0, 'loss': 0, 'draw': 0, 'tf': 0}}
        player_data.update(d)

    analysis, round_num, num_res = [], 0, 0
    
    # Record elapse time for the whole match
    time_start = time.perf_counter_ns()
    
    # Prepare opening start positions for the match
    games = get_game_list(opening_file, log_fn, max_round, randomize_pos)

    total_games = (len(player_data)-1) * len(games) * 2 if reverse_start_side else (len(player_data)-1) * len(games)
    total_games = total_games if gauntlet_color is None else total_games//2
    
    print_match_conditions(len(games), reverse_start_side, opening_file,
                           randomize_pos, parallel, base_time_ms, inc_time_ms,
                           win_adj, win_score_cp, win_score_count, log_fn)
    
    # Run game matches in parallel
    if parallel < 1:
        logger.warning(f'parallel is only {parallel}!, now it set at 1.')
        parallel = 1

    with ProcessPoolExecutor(max_workers=parallel) as executor:
        game_id, round_num = 0, 0
        
        # Submit engine matches as job
        for game in games:                    
            round_num += 1
            sub_round = 0.0
            
            # Generate gauntlet matches, engine 1 is the gauntlet.
            for i in range(len(player_data)):
                m, n = 0, i+1
                
                if gauntlet_color == 'white':
                    m, n = n, m
                
                if i == len(player_data) - 1:
                    break
                
                games_per_pair_per_round = 0
                while True:
                    game_id += 1
                    sub_round += 0.1
                    
                    g = Match(
                        game,
                        player_data[m],
                        player_data[n],
                        round_num + sub_round if reverse_start_side else round_num,
                        total_games, game_id, log_fn,
                        win_adj, win_score_cp, win_score_count, is_engine_log)
                    
                    job = executor.submit(g.start_match)
                    analysis.append(job)
                    games_per_pair_per_round += 1
                    
                    if not reverse_start_side or \
                        gauntlet_color == 'white' or gauntlet_color == 'black':
                        break
                    
                    if games_per_pair_per_round >= 2:
                        break
                    
                    m, n = n, m  # Reverse the side
            
        # Process every game results
        for future in concurrent.futures.as_completed(analysis):
            try:
                game_output = future.result()[0]
                game_num = future.result()[1]
                round_number = future.result()[2]
                game_elapse = future.result()[3]  # nanoseconds
                
                num_res += 1
                
                wp = game_output.headers['White']
                bp = game_output.headers['Black']
                res = game_output.headers['Result']
                try:
                    termi = game_output.headers['Termination']
                except KeyError:
                    termi = 'normal'
                except Exception:
                    logger.exception('Error in getting termination header value!')
                    termi = 'unknown'
                
                # Save games to a file
                print(game_output, file=open(outpgn, 'a'), end='\n\n')
                
                # Update engine score incrementally for result table
                player_data = update_score(game_output, player_data)

                logger.info(f'Done, game: {game_num}, round: {round_number}, elapse: {get_time_h_mm_ss_ms(game_elapse)}')
                logger.info(f'players: {wp} vs {bp}')
                logger.info(f'result: {res} ({termi})')
                
                print_result_table(player_data, num_res, log_fn)

            except Exception:
                logger.exception('Exception in completed analysis.')         
    
    elapse = time.perf_counter_ns() - time_start  # time delta in nanoseconds
    logger.info(f'Match: done, elapse: {get_time_h_mm_ss_ms(elapse)}')
    
    logging.shutdown()
    

if __name__ == '__main__':
    main()
    