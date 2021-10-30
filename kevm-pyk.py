#!/usr/bin/env python3

import argparse
from   datetime import datetime
from   graphviz import Digraph
import hashlib
import os
import subprocess
import sys
import time

from   pyk          import *
from   pyk.__main__ import main, pykArgs, pykCommandParsers

sys.setrecursionlimit(15000000)

lastTime = time.time()

### UPSTREAMABLE

def removeGeneratedCells(constrainedTerm):
    """Remove <generatedTop> and <generatedCounter> from a configuration.

    -   Input: Constrained term which contains <generatedTop> and <generatedCounter>.
    -   Output: Constrained term with those cells removed.
    """
    rule = ( KApply('<generatedTop>', [ KVariable('CONFIG') , KApply('<generatedCounter>', [KVariable('_')]) ])
           , KVariable('CONFIG')
           )
    return rewriteAnywhereWith(rule, constrainedTerm)

def makeDefinition(sents, specModuleName, requireNames, moduleNames):
    module = KFlatModule(specModuleName, moduleNames, sents)
    return KDefinition(specModuleName, [module], requires = [KRequire(name.split('/')[-1]) for name in requireNames])

def isAnonVariable(kast):
    return isKVariable(kast) and kast['name'].startswith('_')

def getCell(constrainedTerm, cellVariable):
    (state, _) = splitConfigAndConstraints(constrainedTerm)
    (_, subst) = splitConfigFrom(state)
    return subst[cellVariable]

def setCell(constrainedTerm, cellVariable, cellValue):
    (state, constraint) = splitConfigAndConstraints(constrainedTerm)
    (config, subst)     = splitConfigFrom(state)
    subst[cellVariable] = cellValue
    return KApply('#And', [substitute(config, subst), constraint])

def structurallyFrameKCell(constrainedTerm):
    kCell = getCell(constrainedTerm, 'K_CELL')
    if isKSequence(kCell) and len(kCell['items']) > 0 and isAnonVariable(kCell['items'][-1]):
        kCell = KSequence(kCell['items'][0:-1] + [ktokenDots])
    return setCell(constrainedTerm, 'K_CELL', kCell)

def applyCellSubst(constrainedTerm, cellSubst):
    (state, constraint) = splitConfigAndConstraints(constrainedTerm)
    (config, subst)     = splitConfigFrom(state)
    for k in cellSubst:
        subst[k] = cellSubst[k]
    return KApply('#And', [substitute(config, subst), constraint])

def removeUselessConstraints(constrainedTerm):
    (state, constraint) = splitConfigAndConstraints(constrainedTerm)
    stateVars           = collectFreeVars(state)
    constraints         = flattenLabel('#And', constraint)
    newConstraints      = []
    for c in constraints:
        if all([v in stateVars for v in collectFreeVars(c)]):
            newConstraints.append(c)
    return buildAssoc(KConstant('#Top'), '#And', [state] + newConstraints)

def removeConstraintsFor(varNames, constrainedTerm):
    (state, constraint) = splitConfigAndConstraints(constrainedTerm)
    constraints         = flattenLabel('#And', constraint)
    newConstraints      = []
    for c in constraints:
        if not any([v in varNames for v in collectFreeVars(c)]):
            newConstraints.append(c)
    return buildAssoc(KConstant('#Top'), '#And', [state] + newConstraints)

def buildRule(ruleId, initConstrainedTerm, finalConstrainedTerm, claim = False, priority = None, keepVars = None):
    (initConfig,  initConstraint)  = splitConfigAndConstraints(initConstrainedTerm)
    (finalConfig, finalConstraint) = splitConfigAndConstraints(finalConstrainedTerm)
    ruleBody                       = pushDownRewrites(KRewrite(initConfig, finalConfig))
    ruleConstrainedTerm            = buildAssoc(KConstant('#Top'), '#And', [ruleBody, initConstraint, finalConstraint])
    ruleConstrainedTerm            = removeUselessConstraints(ruleConstrainedTerm)
    ruleConstrainedTerm            = markUselessVars(ruleConstrainedTerm)
    (ruleBody, ruleConstraint)     = splitConfigAndConstraints(ruleConstrainedTerm)
    ruleRequires                   = simplifyBool(unsafeMlPredToBool(ruleConstraint))
    ruleAtt                        = None if priority is None else KAtt(atts = {'priority': str(priority)})
    if not claim:
        rule = KRule (ruleBody, requires = ruleRequires, att = ruleAtt)
    else:
        rule = KClaim(ruleBody, requires = ruleRequires, att = ruleAtt)
    rule = addAttributes(rule, { 'label': ruleId })
    return minimizeRule(rule, keepVars = keepVars)

def onCells(cellHandler, constrainedTerm):
    """Given an effect and a constrained term, return the effect applied to the cells in the term.

    -   Input: Effect that takes as input a cell name and the contents of the cell, and a constrained term.
    -   Output: Constrained term with the effect applied to each cell.
    """
    (config, constraint) = splitConfigAndConstraints(constrainedTerm)
    constraints          = flattenLabel('#And', constraint)
    (emptyConfig, subst) = splitConfigFrom(config)
    for k in subst:
        newCell = cellHandler(k, subst[k])
        if newCell is not None:
            (term, constraint) = newCell
            subst[k] = term
            if not constraint in constraints:
                constraints.append(constraint)
    return buildAssoc(KConstant('#Top'), '#And', [substitute(emptyConfig, subst)] + constraints)

def getAppliedAxiomList(debugLogFile):
    axioms      = []
    next_axioms = []
    with open(debugLogFile, 'r') as logFile:
        for line in logFile:
            if line.find('DebugTransition') > 0:
                if line.find('after  apply axioms:') > 0:
                    next_axioms.append(line[line.find('after  apply axioms:') + len('after  apply axioms:'):])
                elif len(next_axioms) > 0:
                    axioms.append(next_axioms)
                    next_axioms = []
    return axioms

def abstractTermSafely(kast, baseName = 'V'):
    vnameHash = hashlib.sha256()
    vnameHash.update(str(kast).encode('utf-8'))
    vname = str(vnameHash.hexdigest())[0:8]
    return KVariable(baseName + '_' + vname)

def abstractCell(constrainedTerm, cellName):
    (state, constraint) = splitConfigAndConstraints(constrainedTerm)
    constraints         = flattenLabel('#And', constraint)
    cell                = getCell(state, cellName)
    cellVar             = KVariable('_' + cellName)
    if not isKVariable(cell):
        state = setCell(state, cellName, cellVar)
        constraints.append(KApply('#Equals', [cellVar, cell]))
    return buildAssoc(KConstant('#Top'), '#And', [state] + constraints)

def applyConstraintSubstitutions(constrainedTerm):
    (state, constraint) = splitConfigAndConstraints(constrainedTerm)
    constraints         = flattenLabel('#And', constraint)
    substPattern        = mlEqualsTrue(KApply('_==K_', [KVariable('#VAR'), KVariable('#VAL')]))
    for c in constraints:
        substMatch = match(substPattern, c)
        if substMatch is not None and isKVariable(substMatch['#VAR']):
            state = substitute(state, { substMatch['#VAR']['name'] : substMatch['#VAL'] })
    return buildAssoc(KConstant('#Top'), '#And', [state] + constraints)

class KPrint:
    def __init__(self, kompiledDirectory):
        self.kompiledDirectory = kompiledDirectory
        self.definition        = readKastTerm(self.kompiledDirectory + '/compiled.json')
        self.symbolTable       = buildSymbolTable(self.definition, opinionated = True)

    def prettyPrint(self, kast):
        return prettyPrintKast(kast, self.symbolTable)

    def prettyPrintConstraint(self, constraint):
        return self.prettyPrint(simplifyBool(unsafeMlPredToBool(constraint)))

class KProve(KPrint):
    def __init__(self, kompiledDirectory, mainFileName):
        super(KProve, self).__init__(kompiledDirectory)
        self.directory         = '/'.join(self.kompiledDirectory.split('/')[0:-1])
        self.mainFileName      = mainFileName
        if self.mainFileName.startswith(self.directory + '/'):
            self.mainFileName  = self.mainFileName[len(self.directory + '/'):]
        self.prover            = [ 'kprove' ]
        self.proverArgs        = [ ]
        with open(self.kompiledDirectory + '/backend.txt', 'r') as ba:
            self.backend       = ba.read()
        with open(self.kompiledDirectory + '/mainModule.txt', 'r') as mm:
            self.mainModule    = mm.read()

    def prove(self, specFile, specModuleName, args = [], haskellArgs = [], logAxiomsFile = None, dieOnFail = False):
        logFile = kevm.directory + '/' + claimId.lower() + '-debug.log' if logAxiomsFile is None else logAxiomsFile
        if os.path.exists(logFile):
            os.remove(logFile)
        haskellLogArgs = [ '--log' , logFile , '--log-format'  , 'oneline' , '--log-entries' , 'DebugTransition' ]
        command  = self.prover
        command += [ specFile ]
        command += [ '--backend' , self.backend , '--directory' , self.directory , '-I' , self.directory , '--spec-module' , specModuleName , '--output' , 'json' ]
        command += self.proverArgs
        command += args
        commandEnv                   = os.environ.copy()
        commandEnv['KORE_EXEC_OPTS'] = ' '.join(haskellArgs + haskellLogArgs)
        _notif(' '.join(command))
        process = subprocess.Popen(command, stdout = subprocess.PIPE, stderr = subprocess.PIPE, universal_newlines = True, env = commandEnv)
        try:
            (stdout, stderr) = process.communicate(input = None)
            finalState       = json.loads(stdout)['term']
            if finalState == KConstant('#Top') and len(getAppliedAxiomList(logFile)) == 0:
                _fatal('Proof took zero steps, likely the LHS is invalid: ' + tmpClaim)
            elif dieOnFail:
                _fatal('Unable to prove claim: ' + specFile)
            return finalState
        except:
            sys.stderr.write(stdout + '\n')
            sys.stderr.write(stderr + '\n')
            _fatal('Exiting...', exitCode = rc)

    def proveClaim(self, claim, claimId, args = [], haskellArgs = [], logAxiomsFile = None, dieOnFail = False):
        tmpClaim      = self.directory + '/' + claimId.lower() + '-spec.k'
        tmpModuleName = claimId.upper() + '-SPEC'
        with open(tmpClaim, 'w') as tc:
            claimModule     = KFlatModule(tmpModuleName, [self.mainModule], [claim])
            claimDefinition = KDefinition(tmpModuleName, [claimModule], requires = [KRequire(self.mainFileName)])
            tc.write(_genFileTimestamp() + '\n')
            tc.write(self.prettyPrint(claimDefinition) + '\n\n')
            tc.flush()
        return self.prove(tmpClaim, tmpModuleName, args = args, haskellArgs = haskellArgs, logAxiomsFile = logAxiomsFile, dieOnFail = dieOnFail)

### Utilities

def _notif(msg):
    global lastTime
    curTime  = time.time()
    diffTime = curTime - lastTime
    lastTime = curTime
    sys.stderr.write('== ' + sys.argv[0].split('/')[-1] + ' ' + str(datetime.now()) + ' [+' + '{0:8.2f}'.format(diffTime) + ']: ' + msg + '\n')
    sys.stderr.flush()

def _warning(msg):
    _notif('[WARNING] ' + msg)

def _fatal(msg, exitCode = 1):
    _notif('[FATAL] ' + msg)
    raise('Quitting')

def _genFileTimestamp(comment = '//'):
    return comment + ' This file generated by: ' + sys.argv[0] + '\n' + comment + ' ' + str(datetime.now()) + '\n'

### KEVM Stuff

boolToken             = lambda b:       KToken(str(b).lower(), 'Bool')
intToken              = lambda i:       KToken(str(i), 'Int')
stringToken           = lambda s:       KToken('"' + s + '"', 'String')
ltInt                 = lambda i1, i2:  KApply('_<Int_', [i1, i2])
leInt                 = lambda i1, i2:  KApply('_<=Int_', [i1, i2])
rangeUInt256          = lambda i:       KApply('#rangeUInt(_,_)_EVM-TYPES_Bool_Int_Int', [intToken(256), i])
rangeAddress          = lambda i:       KApply('#rangeAddress(_)_EVM-TYPES_Bool_Int', [i])
sizeByteArray         = lambda ba:      KApply('#sizeByteArray(_)_EVM-TYPES_Int_ByteArray', [ba])
infGas                = lambda g:       KApply('infGas', [g])
computeValidJumpDests = lambda p:       KApply('#computeValidJumpDests(_)_EVM_Set_ByteArray', [p])
binRuntime            = lambda c:       KApply('#binRuntime', [c])
mlEqualsTrue          = lambda b:       KApply('#Equals', [boolToken(True), b])
abiCallData           = lambda n, args: KApply('#abiCallData', [stringToken(n)] + args)
abiAddress            = lambda a:       KApply('#address(_)_EVM-ABI_TypedArg_Int', [KVariable(a)])
abiUInt256            = lambda i:       KApply('#uint256(_)_EVM-ABI_TypedArg_Int', [KVariable(i)])
bytesAppend           = lambda b1, b2:  KApply('_++__EVM-TYPES_ByteArray_ByteArray_ByteArray', [b1, b2])

def kevmAccountCell(id, balance, code, storage, origStorage, nonce):
    return KApply('<account>', [ KApply('<acctID>'      , [id])
                               , KApply('<balance>'     , [balance])
                               , KApply('<code>'        , [code])
                               , KApply('<storage>'     , [storage])
                               , KApply('<origStorage>' , [origStorage])
                               , KApply('<nonce>'       , [nonce])
                               ]
                 )

def kevmSymbolTable(symbolTable):
    symbolTable['_Set_']                                                     = paren(symbolTable['_Set_'])
    symbolTable['_AccountCellMap_']                                          = paren(lambda a1, a2: a1 + '\n' + a2)
    symbolTable['AccountCellMapItem']                                        = lambda k, v: v
    symbolTable['_[_:=_]_EVM-TYPES_Memory_Memory_Int_ByteArray']             = lambda m, k, v: m + ' [ '  + k + ' := (' + v + '):ByteArray ]'
    symbolTable['_[_.._]_EVM-TYPES_ByteArray_ByteArray_Int_Int']             = lambda m, s, w: '( ' + m + ' [ ' + s + ' .. ' + w + ']):ByteArray'
    symbolTable['_<Word__EVM-TYPES_Int_Int_Int']                             = paren(lambda a1, a2: '(' + a1 + ') <Word ('  + a2 + ')' )
    symbolTable['_>Word__EVM-TYPES_Int_Int_Int']                             = paren(lambda a1, a2: '(' + a1 + ') >Word ('  + a2 + ')' )
    symbolTable['_<=Word__EVM-TYPES_Int_Int_Int']                            = paren(lambda a1, a2: '(' + a1 + ') <=Word (' + a2 + ')' )
    symbolTable['_>=Word__EVM-TYPES_Int_Int_Int']                            = paren(lambda a1, a2: '(' + a1 + ') >=Word (' + a2 + ')' )
    symbolTable['_==Word__EVM-TYPES_Int_Int_Int']                            = paren(lambda a1, a2: '(' + a1 + ') ==Word (' + a2 + ')' )
    symbolTable['_s<Word__EVM-TYPES_Int_Int_Int']                            = paren(lambda a1, a2: '(' + a1 + ') s<Word (' + a2 + ')' )
    symbolTable['EVMC_UNDEFINED_INSTRUCTION_NETWORK_ExceptionalStatusCode']  = lambda: 'EVMC_UNDEFINED_INSTRUCTION'
    symbolTable['EVMC_SUCCESS_NETWORK_EndStatusCode']                        = lambda: 'EVMC_SUCCESS'
    symbolTable['EVMC_STATIC_MODE_VIOLATION_NETWORK_ExceptionalStatusCode']  = lambda: 'EVMC_STATIC_MODE_VIOLATION'
    symbolTable['EVMC_STACK_UNDERFLOW_NETWORK_ExceptionalStatusCode']        = lambda: 'EVMC_STACK_UNDERFLOW'
    symbolTable['EVMC_STACK_OVERFLOW_NETWORK_ExceptionalStatusCode']         = lambda: 'EVMC_STACK_OVERFLOW'
    symbolTable['EVMC_REVERT_NETWORK_EndStatusCode']                         = lambda: 'EVMC_REVERT'
    symbolTable['EVMC_REJECTED_NETWORK_StatusCode']                          = lambda: 'EVMC_REJECTED'
    symbolTable['EVMC_PRECOMPILE_FAILURE_NETWORK_ExceptionalStatusCode']     = lambda: 'EVMC_PRECOMPILE_FAILURE'
    symbolTable['EVMC_OUT_OF_GAS_NETWORK_ExceptionalStatusCode']             = lambda: 'EVMC_OUT_OF_GAS'
    symbolTable['EVMC_INVALID_MEMORY_ACCESS_NETWORK_ExceptionalStatusCode']  = lambda: 'EVMC_INVALID_MEMORY_ACCESS'
    symbolTable['EVMC_INVALID_INSTRUCTION_NETWORK_ExceptionalStatusCode']    = lambda: 'EVMC_INVALID_INSTRUCTION'
    symbolTable['EVMC_INTERNAL_ERROR_NETWORK_StatusCode']                    = lambda: 'EVMC_INTERNAL_ERROR'
    symbolTable['EVMC_FAILURE_NETWORK_ExceptionalStatusCode']                = lambda: 'EVMC_FAILURE'
    symbolTable['EVMC_CALL_DEPTH_EXCEEDED_NETWORK_ExceptionalStatusCode']    = lambda: 'EVMC_CALL_DEPTH_EXCEEDED'
    symbolTable['EVMC_BALANCE_UNDERFLOW_NETWORK_ExceptionalStatusCode']      = lambda: 'EVMC_BALANCE_UNDERFLOW'
    symbolTable['EVMC_BAD_JUMP_DESTINATION_NETWORK_ExceptionalStatusCode']   = lambda: 'EVMC_BAD_JUMP_DESTINATION'
    symbolTable['EVMC_ACCOUNT_ALREADY_EXISTS_NETWORK_ExceptionalStatusCode'] = lambda: 'EVMC_ACCOUNT_ALREADY_EXISTS'
    return symbolTable

def kevmUndoMacros(constraint):
    constraints  = flattenLabel('#And', constraint)
    replaceRules = []
    for v in collectFreeVars(constraint):
        le0   = mlEqualsTrue(leInt(intToken(0), KVariable(v)))
        lt256 = mlEqualsTrue(ltInt(KVariable(v), intToken(2 ** 256)))
        lt160 = mlEqualsTrue(ltInt(KVariable(v), intToken(2 ** 160)))
        if le0 in constraints and lt256 in constraints:
            constraints.append(mlEqualsTrue(rangeUInt256(KVariable(v))))
            replaceRules.append((le0,   KConstant('#Top')))
            replaceRules.append((lt256, KConstant('#Top')))
        elif le0 in constraints and lt160 in constraints:
            constraints.append(mlEqualsTrue(rangeAddress(KVariable(v))))
            replaceRules.append((le0,   KConstant('#Top')))
            replaceRules.append((lt160, KConstant('#Top')))
    def _replacer(k):
        for r in replaceRules:
            k = replaceAnywhereWith(r, k)
        return k
    constraint = buildAssoc(KConstant('#Top'), '#And', [ _replacer(c) for c in constraints ])
    return constraint

def kevmMakeExecutable(initConstrainedTerm, finalConstrainedTerm):
    (initState,  initConstraint)  = splitConfigAndConstraints(initConstrainedTerm)
    (finalState, finalConstraint) = splitConfigAndConstraints(finalConstrainedTerm)
    initConstraints               = flattenLabel('#And', initConstraint)
    finalConstraints              = flattenLabel('#And', finalConstraint)

    gasInit  = getCell(initState,  'GAS_CELL')
    gasFinal = getCell(finalState, 'GAS_CELL')
    if isKApply(gasInit) and gasInit['label'] == 'infGas':
        initState = setCell(initState, 'GAS_CELL', gasInit['args'][0])
    if isKApply(gasFinal) and gasFinal['label'] == 'infGas':
        finalState = setCell(finalState, 'GAS_CELL', gasFinal['args'][0])
        finalConstraints.append(mlEqualsTrue(leInt(intToken(0), gasFinal['args'][0])))

    initConstrainedTerm  = buildAssoc(KConstant('#Top'), '#And', [initState]  + initConstraints)
    finalConstrainedTerm = buildAssoc(KConstant('#Top'), '#And', [finalState] + finalConstraints)
    finalConstrainedTerm = applyConstraintSubstitutions(finalConstrainedTerm)
    return (initConstrainedTerm, finalConstrainedTerm)

def kevmSanitizeConfig(initConstrainedTerm):

    def _parseableVarNames(cellName, term):
        if isAnonVariable(term) and (term['name'][1:].isdigit() or term['name'][1:].startswith('DotVar')):
            return (KVariable('_' + cellName), KConstant('#Top'))
        return None

    bytesConstraints = []
    def _parseableBytesTokens(_kast):
        if isKToken(_kast) and _kast['sort'] == 'Bytes' and _kast['token'].startswith('b"') and _kast['token'].endswith('"'):
            bytesVariable = abstractTermSafely(_kast, baseName = 'BYTES')
            bytesConstraints.append(mlEqualsTrue(KApply('_==K_', [bytesVariable, KApply('String2Bytes(_)_BYTES-HOOKED_Bytes_String', [stringToken(_kast['token'][2:-1])])])))
            return bytesVariable
        return _kast

    def _removeCellMapDefinedness(_kast):
        if isKApply(_kast) and _kast['label'].endswith('CellMap:in_keys') and isAnonVariable(_kast['args'][1]):
            return boolToken(False)
        return _kast

    newConstrainedTerm = onCells(_parseableVarNames, initConstrainedTerm)
    newConstrainedTerm = traverseBottomUp(newConstrainedTerm, _parseableBytesTokens)
    newConstrainedTerm = traverseBottomUp(newConstrainedTerm, _removeCellMapDefinedness)
    newConstrainedTerm = buildAssoc(KConstant('#Top'), '#And', [newConstrainedTerm] + bytesConstraints)

    (state, constraint) = splitConfigAndConstraints(newConstrainedTerm)
    state               = removeGeneratedCells(state)
    constraint          = buildAssoc(KConstant('#Top'), '#And', dedupeClauses(flattenLabel('#And', constraint)))
    constraint          = kevmUndoMacros(constraint)
    return buildAssoc(KConstant('#Top'), '#And', [state, constraint])

def kevmGetBasicBlocks(kevm, initConstrainedTerm, claimId, maxDepth = 1000, isTerminal = None):
    claim       = buildEmptyClaim(initConstrainedTerm, claimId)
    logFileName = kevm.directory + '/' + claimId.lower() + '-debug.log'
    nextState   = kevm.proveClaim( claim , claimId , args = [ '--branching-allowed' , '1' , '--depth' , str(maxDepth) ] , logAxiomsFile = logFileName )
    if nextState == KConstant('#Top'):
        _fatal('Proved claim for generating basic block, use unproveable claims for summaries.')
    nextStates = [ kevmSanitizeConfig(structurallyFrameKCell(s)) for s in flattenLabel('#Or', nextState) ]

    branching = False
    depth     = 0
    for axioms in getAppliedAxiomList(logFileName):
        if len(axioms) > 1:
            branching = True
            depth += 1
            break
        else:
            depth += 1

    if isTerminal is not None and isTerminal(nextStates[0]):
        depth -= 1
        nextState  = kevm.proveClaim(claim, claimId, args = ['--depth', str(depth)])
        nextStates = [ kevmSanitizeConfig(structurallyFrameKCell(s)) for s in flattenLabel('#Or', nextState) ]

    _notif('Found ' + str(len(nextStates)) + ' basic blocks for ' + claimId + ' at depth ' + str(depth) + '.')

    (_, initConstraint) = splitConfigAndConstraints(initConstrainedTerm)
    initConstraints     = flattenLabel('#And', initConstraint)
    nextStates          = [ abstractCell(ns, 'CALLVALUE_CELL') for ns in nextStates ]
    statesAndConstraint = flattenLabel('#And', propagateUpConstraints(buildAssoc(KConstant('#Bottom'), '#Or', nextStates)))
    nextStates          = flattenLabel('#Or', statesAndConstraint[0])
    commonConstraints   = statesAndConstraint[1:]

    newStatesAndConstraints = []
    for ns in nextStates:
        nsAndConstraint = flattenLabel('#And', ns)
        ns              = buildAssoc(KConstant('#Top'), '#And', dedupeClauses(nsAndConstraint + commonConstraints))
        newConstraint   = buildAssoc(KConstant('#Top'), '#And', dedupeClauses([ c for c in nsAndConstraint[1:] if c not in initConstraints ]))
        newStatesAndConstraints.append((ns, newConstraint))

    return (depth, newStatesAndConstraints)

### KEVM Specialization

def abstract(constrainedTerm):

    cellSubst = { 'GAS_CELL'        : infGas(KVariable('_GAS_CELL'))
                , 'MEMORYUSED_CELL' : KVariable('MEMORYUSED_CELL')
                }

    newVars = [ '_GAS_CELL' , 'MEMEORYUSED_CELL' ]

    newConstraints = [ mlEqualsTrue(rangeUInt256(KVariable('MEMORYUSED_CELL'))) ]

    newConstrainedTerm = removeConstraintsFor(newVars, constrainedTerm)
    newConstrainedTerm = applyCellSubst(newConstrainedTerm, cellSubst)
    newConstrainedTerm = removeUselessConstraints(newConstrainedTerm)
    newConstrainedTerm = buildAssoc(KConstant('Top'), '#And', [newConstrainedTerm] + newConstraints)
    newConstrainedTerm = markUselessVars(newConstrainedTerm)

    return newConstrainedTerm

def loopAbstract(constrainedTerm):

    wordStack          = getCell(constrainedTerm, 'WORDSTACK_CELL')
    wordStackFreshVars = []
    if not isKVariable(wordStack):
        wordStackItems = flattenLabel('_:__EVM-TYPES_WordStack_Int_WordStack', wordStack)
        if not (len(wordStackItems) > 0 and wordStackItems[-1] == KConstant('.WordStack_EVM-TYPES_WordStack')):
            _fatal('Cannot abstract wordstack: ' + str(wordStack))
        newWordStackItems = []
        for (i, w) in enumerate(wordStackItems[0:-1]):
            if isKVariable(w):
                newWordStackItems.append(w)
            elif isKToken(w):
                newWordStackItems.append(w)
            else:
                newVar = abstractTermSafely(w, baseName = 'W')
                wordStackFreshVars.append(newVar['name'])
                newWordStackItems.append(newVar)
        wordStack = buildCons(KConstant('.WordStack_EVM-TYPES_WordStack'), '_:__EVM-TYPES_WordStack_Int_WordStack', newWordStackItems)

    cellSubst = { 'WORDSTACK_CELL'  : wordStack
                , 'LOCALMEM_CELL'   : KVariable('_LOCALMEM_CELL')
                }

    newVars = [ '_LOCALMEM_CELL' ] + wordStackFreshVars

    newConstraints = [ mlEqualsTrue(rangeUInt256(KVariable(v))) for v in wordStackFreshVars ]

    newConstrainedTerm = removeConstraintsFor(newVars, constrainedTerm)
    newConstrainedTerm = applyCellSubst(newConstrainedTerm, cellSubst)
    newConstrainedTerm = removeUselessConstraints(newConstrainedTerm)
    newConstrainedTerm = buildAssoc(KConstant('Top'), '#And', [newConstrainedTerm] + newConstraints)
    newConstrainedTerm = markUselessVars(newConstrainedTerm)

    return newConstrainedTerm

def subsumes(constrainedTerm1, constrainedTerm2):
    # Could implement this with the search-final-state-matching check used for LLVM backend? Does it work with constraints?
    if all([getCell(constrainedTerm1, cn) == getCell(constrainedTerm2, cn) for cn in ['PC_CELL', 'K_CELL']]):
        wordStack1 = getCell(constrainedTerm1, 'WORDSTACK_CELL')
        wordStack2 = getCell(constrainedTerm2, 'WORDSTACK_CELL')
        if match(wordStack1, wordStack2) is not None:
            return True
    return False

def isTerminal(constrainedTerm):
    return getCell(constrainedTerm, 'K_CELL') == KSequence([KConstant('#halt_EVM_KItem'), ktokenDots])

def buildTerminal(constrainedTerm):
    (state, _)      = splitConfigAndConstraints(constrainedTerm)
    (config, subst) = splitConfigFrom(state)
    for s in subst:
        subst[s] = KVariable('?_')
    subst['K_CELL']          = KSequence([KConstant('#halt_EVM_KItem'), ktokenDots])
    subst['STATUSCODE_CELL'] = KConstant('.StatusCode_NETWORK_StatusCode')
    return substitute(config, subst)

### Summarization Utilities

def buildEmptyClaim(initConstrainedTerm, claimId):
    finalConstrainedTerm = buildTerminal(initConstrainedTerm)
    return buildRule(claimId, initConstrainedTerm, finalConstrainedTerm, claim = True, keepVars = collectFreeVars(initConstrainedTerm))

def writeCFG(cfg, semantics, graphvizFile = None):
    states = set(list(cfg['graph'].keys()) + cfg['init'] + cfg['terminal'] + cfg['frontier'] + cfg['stuck'])
    cfgLines = [ '// CFG:'
               , '//     states:      ' + ', '.join([str(s) for s in states])
               , '//     init:        ' + ', '.join([str(s) for s in cfg['init']])
               , '//     terminal:    ' + ', '.join([str(s) for s in cfg['terminal']])
               , '//     frontier:    ' + ', '.join([str(s) for s in cfg['frontier']])
               , '//     stuck:       ' + ', '.join([str(s) for s in cfg['stuck']])
               , '//     transitions:'
               ]
    for initStateId in cfg['graph']:
        for finState in cfg['graph'][initStateId]:
            (finalStateId, label, depth) = (finState['successor'], semantics.prettyPrintConstraint(finState['constraint']), finState['depth'])
            cfgLines.append('//         ' + '{0:>3}'.format(initStateId) + ' -> ' + '{0:>3}'.format(finalStateId) + ' [' + '{0:>5}'.format(depth) + ' steps]: ' + label)
            if 'accountUpdate' in finState:
                cfgLines.append('\n//                                   ' + '\n//                                   '.join(semantics.prettyPrint(finState['accountUpdate']).split('\n')))
    if graphvizFile is not None:
        graph = Digraph()
        for s in states:
            labels = [str(s) + ':']
            for l in ['init', 'terminal', 'frontier', 'stuck']:
                if s in cfg[l]:
                    labels.append(l)
            label = ' '.join(labels)
            graph.node(str(s), label = label)
        for s in cfg['graph'].keys():
            for finState in cfg['graph'][s]:
                (f, id, d) = (finState['successor'], semantics.prettyPrintConstraint(finState['constraint']), finState['depth'])
                label = id
                if d != 1:
                    label = label + ': ' + str(d) + ' steps'
                if 'accountUpdate' in finState:
                    label = label + '\n  ' + '\n  '.join(semantics.prettyPrint(finState['accountUpdate']).split('\n'))
                graph.edge(str(s), str(f), label = '  ' + label + '        ')
        graph.render(graphvizFile)
        _notif('Wrote graphviz rendering of CFG: ' + graphvizFile + '.pdf')
    return '\n'.join(cfgLines)

### KEVM Summarizer

def buildInitState(contractName, constrainedTerm):
    accountCell = kevmAccountCell(KVariable('ACCT_ID'), KVariable('ACCT_BALANCE'), KVariable('_ACCT_CODE'), KVariable('_ACCT_STORAGE'), KVariable('_ACCT_ORIGSTORAGE'), KVariable('ACCT_NONCE'))
    cellSubst   = { 'K_CELL'         : KSequence([KConstant('#execute_EVM_KItem'), ktokenDots])
                  , 'ID_CELL'        : KVariable('ACCT_ID')
                  , 'CALLER_CELL'    : KVariable('CALLER_ID')
                  , 'CALLDATA_CELL'  : KVariable('CALLDATA_CELL')
                  , 'GAS_CELL'       : infGas(KVariable('_GAS_CELL'))
                  , 'PC_CELL'        : KToken('0', 'Int')
                  , 'WORDSTACK_CELL' : KConstant('.WordStack_EVM-TYPES_WordStack')
                  , 'LOCALMEM_CELL'  : KVariable('LOCALMEM_CELL')
                  , 'ACCOUNTS_CELL'  : KApply('_AccountCellMap_', [KApply('AccountCellMapItem', [KApply('<acctID>', [KVariable('ACCT_ID')]), accountCell]), KVariable('_ACCOUNTS')])
                  }
    constraints = [ mlEqualsTrue(rangeAddress(KVariable('ACCT_ID')))
                  , mlEqualsTrue(rangeAddress(KVariable('CALLER_ID')))
                  , mlEqualsTrue(rangeUInt256(KVariable('ACCT_BALANCE')))
                  , mlEqualsTrue(rangeUInt256(KVariable('ACCT_NONCE')))
                  , mlEqualsTrue(ltInt(sizeByteArray(KVariable('CALLDATA_CELL')), intToken(2 ** 256)))
                  ]
    return buildAssoc(KConstant('#Top'), '#And', [applyCellSubst(constrainedTerm, cellSubst)] + constraints)

def kevmTransitionLabel(successor, initConstrainedTerm, finalConstrainedTerm, newConstraint, depth):
    label = { 'successor': successor, 'constraint': newConstraint, 'depth': depth }
    initAccounts  = getCell(initConstrainedTerm,  'ACCOUNTS_CELL')
    finalAccounts = getCell(finalConstrainedTerm, 'ACCOUNTS_CELL')
    if initAccounts != finalAccounts:
        accountUpdate = pushDownRewrites(KRewrite(initAccounts, finalAccounts))
        accountUpdate = uselessVarsToDots(accountUpdate)
        accountUpdate = collapseDots(accountUpdate)
        label['accountUpdate'] = accountUpdate
    return label

def kevmWriteStateToFile(kevm, contractName, stateId, state):
    stateFileName = kevm.directory + '/' + contractName.lower() + '-state-' + stateId
    with open(stateFileName + '.json', 'w') as f:
        f.write(json.dumps(state, indent = 2))
    with open(stateFileName + '.k', 'w') as f:
        f.write(_genFileTimestamp() + '\n')
        f.write(kevm.prettyPrint(state))
    _notif('Wrote state files: ' + stateFileName + '.{k,json}')

def kevmSummarize( kevm
                 , initState
                 , contractName
                 , verify = False
                 , maxBlocks = None
                 , maxDepth = 1000
                 , startOffset = 0
                 , graphvizFile = None
                 ):

    intermediateClaimsFile   = kevm.directory + '/' + contractName.lower() + '-basic-blocks-spec.k'
    intermediateClaimsModule = contractName.upper() + '-BASIC-BLOCKS-SPEC'
    cfgFile                  = kevm.directory + '/' + contractName.lower() + '-cfg.json'

    frontier        = [(startOffset + i, ct) for (i, ct) in enumerate(flattenLabel('#Or', initState))]
    seenStates      = []
    newClaims       = []
    newRules        = []
    cfg             = {}
    cfg['graph']    = {}
    cfg['init']     = [i for (i, _) in frontier]
    cfg['terminal'] = []
    cfg['frontier'] = [i for (i, _) in frontier]
    cfg['stuck']    = []
    nextStateId     = startOffset + len(frontier)
    writtenStates   = []

    while len(frontier) > 0 and (maxBlocks is None or len(newClaims) < maxBlocks):
        (initStateId, initState)     = frontier.pop(0)
        initState                    = abstract(initState)
        (initConfig, initConstraint) = splitConfigAndConstraints(initState)
        initConstraints              = flattenLabel('#And', initConstraint)
        if initStateId not in cfg['graph']:
            cfg['graph'][initStateId] = []
        seenStates.append((initStateId, initState))
        if initStateId not in writtenStates:
            kevmWriteStateToFile(kevm, contractName, str(initStateId), initState)
            writtenStates.append(initStateId)
        claimId                           = contractName.upper() + '-GEN-' + str(initStateId) + '-TO-MAX' + str(maxDepth)
        (depth, nextStatesAndConstraints) = kevmGetBasicBlocks( kevm
                                                              , initState
                                                              , claimId
                                                              , maxDepth = maxDepth
                                                              , isTerminal = isTerminal
                                                              )
        for (i, (finalState, newConstraint)) in enumerate(nextStatesAndConstraints):
            finalStateId = nextStateId
            nextStateId  = nextStateId + 1
            if finalStateId not in writtenStates:
                kevmWriteStateToFile(kevm, contractName, str(finalStateId), finalState)
                writtenStates.append(finalStateId)

            cfg['graph'][initStateId].append(kevmTransitionLabel(finalStateId, initState, finalState, newConstraint, depth))
            if isTerminal(finalState):
                cfg['terminal'].append(finalStateId)
            elif len(nextStatesAndConstraints) == 1 and depth != maxDepth:
                cfg['stuck'].append(finalStateId)
            else:
                subsumed = False
                for (j, seen) in seenStates:
                    if subsumes(seen, finalState):
                        subsumed = True
                        if finalStateId not in cfg['graph']:
                            cfg['graph'][finalStateId] = []
                        cfg['graph'][finalStateId].append(kevmTransitionLabel(j, finalState, seen, newConstraint, 0))
                if not subsumed:
                    frontier.append((finalStateId, finalState))
            cfg['frontier'] = [i for (i, _) in frontier]
            if i < len(nextStatesAndConstraints) - 1:
                cfg['frontier'].insert(0, initStateId)

            (initState, finalState) = kevmMakeExecutable(initState, finalState)
            basicBlockId = contractName.upper() + '-BASIC-BLOCK-' + str(initStateId) + '-TO-' + str(finalStateId)
            newClaim     = buildRule(basicBlockId, initState, finalState, claim = True)
            newClaims.append(newClaim)
            if verify:
                kevm.proveClaim(newClaim, basicBlockId, dieOnFail = True)
                _notif('Verified claim: ' + basicBlockId)
            newRule = buildRule(basicBlockId, initState, finalState, claim = False, priority = 35)
            newRules.append(newRule)

            with open(intermediateClaimsFile, 'w') as intermediate:
                claimDefinition = makeDefinition(newClaims, intermediateClaimsModule, [kevm.mainFileName], [kevm.mainModule])
                intermediate.write(_genFileTimestamp() + '\n')
                intermediate.write(kevm.prettyPrint(claimDefinition) + '\n\n')
                intermediate.write(writeCFG(cfg, kevm, graphvizFile = graphvizFile) + '\n')
                intermediate.flush()
                _notif('Wrote updated claims file: ' + intermediateClaimsFile)

            with open(cfgFile, 'w') as f:
                f.write(json.dumps(cfg))
                _notif('Wrote updated cfg file: ' + cfgFile)

    return (newRules, cfg)

### Main Functionality

summarizeArgs = pykCommandParsers.add_parser('summarize', help = 'Given a disjunction of states, build the true halting spec for each state.')
summarizeArgs.add_argument('init-term' , type = argparse.FileType('r'), help = 'JSON representation of initial state.')
summarizeArgs.add_argument('contract-name' , type = str, help = 'Name of contract to summarize.')
summarizeArgs.add_argument('main-module-file' , type = str, help = 'Name of main verification file.')
summarizeArgs.add_argument('-n', '--max-blocks', type = int, nargs = '?', help = 'Maximum number of basic block summarize to generate.')
summarizeArgs.add_argument('--resume-from-state', type = int, nargs = '?', help = 'Start from saved state file in summarization directory.')
summarizeArgs.add_argument('--verify', default = True, action = 'store_true', help = 'Prove generated claims before outputting them.')
summarizeArgs.add_argument('--no-verify', dest = 'verify', action = 'store_false', help = 'Do not prove generated claims before outputting them.')
summarizeArgs.add_argument('--graphviz', default = False, action = 'store_true', help = 'Write graphviz rendering of CFG.')
summarizeArgs.add_argument('-o', '--output', type = argparse.FileType('w'), default = '-')

def kevmPykMain(args, kompiled_dir):

    if args['command'] == 'summarize':

        mainFileName       = args['main-module-file']
        directory          = '/'.join(mainFileName.split('/')[0:-1])
        contractName       = args['contract-name']
        summaryRulesModule = contractName.upper() + '-BASIC-BLOCKS'
        maxBlocks          = None if 'max_blocks' not in args else args['max_blocks']
        resumeFromState    = None if 'max_blocks' not in args else args['resume_from_state']
        graphvizFile       = None if not args['graphviz']     else directory + '/' + contractName.lower() + '-basic-blocks'

        kevm             = KProve(kompiled_dir, mainFileName)
        kevm.symbolTable = kevmSymbolTable(kevm.symbolTable)
        kevm.prover      = [ 'kevm' , 'prove' ]
        kevm.proverArgs  = [ '--provex' , '--boundary-cells' , 'k,pc' ]

        if resumeFromState is None:
            initState = buildInitState(contractName, json.loads(args['init-term'].read())['term'])
        else:
            with open(directory + '/' + contractName.lower() + '-state-' + str(args['resume_from_state']) + '.json', 'r') as resumeState:
                initState = json.loads(resumeState.read())
        initState = kevmSanitizeConfig(initState)

        (newRules, cfg)   = kevmSummarize( kevm
                                         , initState
                                         , contractName
                                         , verify = args['verify']
                                         , maxBlocks = maxBlocks
                                         , startOffset = resumeFromState if resumeFromState is not None else 0
                                         , graphvizFile = graphvizFile
                                         )
        summaryDefinition = makeDefinition(newRules, summaryRulesModule, ['edsl.md', 'lemmas/infinite-gas.k'], ['EDSL', 'INFINITE-GAS'])

        args['output'].write(_genFileTimestamp() + '\n')
        args['output'].write(kevm.prettyPrint(summaryDefinition) + '\n\n')
        args['output'].write(writeCFG(cfg, kevm, graphvizFile = graphvizFile) + '\n')
        args['output'].flush()

if __name__ == '__main__':
    main(pykArgs, extraMain = kevmPykMain)
