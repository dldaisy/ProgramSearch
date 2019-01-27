from pointerNetwork import *
from utilities import *

import math
import numpy as np

import torch.nn as nn
import torch
import torch.nn.functional as F
import torch.optim as optimization
from torch.autograd import Variable
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence


class ProgramGraph:
    """A program graph is a state in the search space"""
    def __init__(self, nodes):
        self.nodes = nodes if isinstance(nodes, frozenset) else frozenset(nodes)

    @staticmethod
    def fromRoot(r):
        ns = set()
        def reachable(n):
            if n in ns: return
            ns.add(n)
            for c in n.children():
                reachable(c)
        reachable(r)
        return ProgramGraph(ns)

    def __len__(self): return len(self.nodes)

    def prettyPrint(self):
        index2node = []
        node2index = {}
        index2code = {}
        def getIndex(n):
            if n in node2index: return node2index[n]
            serialization = [ t if isinstance(t, str) else f"${getIndex(t)}"
                              for t in n.serialize() ]
            myIndex = len(index2node)
            index2node.append(n)
            index2code[myIndex] = "(" + " ".join(serialization) + ")"
            node2index[n] = myIndex            
            return myIndex
        for n in self.nodes: getIndex(n)
        return "\n".join( f"${i} <- {index2code[i]}"
                          for i in range(len(index2node)))
                          
            
            

    def extend(self, newNode):
        return ProgramGraph(self.nodes | {newNode})

    def objects(self):
        return self.nodes

    def policyOracle(self, targetGraph):
        missingNodes = targetGraph.nodes - self.nodes
        for n in missingNodes:
            if all( child in self.nodes for child in n.children() ):
                yield n

    def distanceOracle(self, targetGraph):
        return len(self.nodes^targetGraph.nodes)


        
class ProgramPointerNetwork(Module):
    """A network that looks at the objects in a ProgramGraph and then predicts what to add to the graph"""
    def __init__(self, objectEncoder, specEncoder, DSL, H=256):
        """
        specEncoder: Module that encodes spec to initial hidden state of RNN
        objectEncoder: Module that encodes (spec, object) to features we attend over
        """
        super(ProgramPointerNetwork, self).__init__()

        self.DSL = DSL
        self.objectEncoder = objectEncoder
        self.specEncoder = specEncoder
        self.decoder = LineDecoder(DSL.lexicon + ["RETURN"],
                                   encoderDimensionality=objectEncoder.outputDimensionality,
                                   H=H)
        self._initialHidden = nn.Sequential(
            nn.Linear(objectEncoder.outputDimensionality + specEncoder.outputDimensionality, H),
            nn.ReLU())

        self._distance = nn.Sequential(
            nn.Linear(objectEncoder.outputDimensionality + specEncoder.outputDimensionality, H),
            nn.ReLU(),
            nn.Linear(H, 1),
            nn.ReLU())

        self.finalize()

    def initialHidden(self, objectEncodings, specEncoding):
        if objectEncodings is None:
            objectEncodings = self.device(torch.zeros(self.objectEncoder.outputDimensionality))
        else:
            objectEncodings = objectEncodings.max(0)[0]
        return self._initialHidden(torch.cat([specEncoding, objectEncodings]))

    def distance(self, objectEncodings, specEncoding):
        """Returns a 1-dimensional tensor which should be the sum of (# objects to create) + (# spurious objects created)"""
        if objectEncodings is None:
            objectEncodings = self.device(torch.zeros(self.objectEncoder.outputDimensionality))
        else:
            objectEncodings = objectEncodings.max(0)[0]

        return self._distance(torch.cat([specEncoding, objectEncodings]))

    def gradientStep(self, optimizer, spec, currentGraph, goalGraph):
        """Returns (policy loss, distance loss)"""
        self.zero_grad()
        
        optimalMoves = list(currentGraph.policyOracle(goalGraph))
        if len(optimalMoves) == 0:
            optimalMoves = [['RETURN']]
            finalMove = True
        else:
            finalMove = False
            
        objects = currentGraph.objects()
        object2pointer = {o: Pointer(i)
                          for i,o in enumerate(objects) }

        if len(objects) > 0:
            objectEncodings = torch.stack([ self.objectEncoder(spec, o.execute())
                                          for o in objects])
        else:
            objectEncodings = self.device(torch.zeros((1, self.objectEncoder.outputDimensionality)))

        se = self.specEncoder(spec)
        h0 = self.initialHidden(objectEncodings, se)

        def substitutePointers(serialization):
            return [token if isinstance(token,str) else object2pointer[token]
                    for token in serialization]

        targetLines = [substitutePointers(m.serialize()) if not finalMove else m
                       for m in optimalMoves]
        targetLikelihoods = [self.decoder.logLikelihood(h0, targetLine, objectEncodings if len(objects) > 0 else None)
                             for targetLine in targetLines]
        policyLoss = -torch.logsumexp(torch.cat([l.view(1) for l in targetLikelihoods ]), dim=0)


        actualDistance = currentGraph.distanceOracle(goalGraph)
        predictedDistance = self.distance(objectEncodings, se)
        distanceLoss = (predictedDistance - float(actualDistance))**2
        
        (policyLoss + distanceLoss).backward()
        optimizer.step()
        return policyLoss.data.item(), distanceLoss.data.item()

    def gradientStepTrace(self, optimizer, spec, goalGraph):
        """Returns ([policy losses], [distance losses])"""
        self.zero_grad()
        
        currentGraph = ProgramGraph([])
        policyLosses, distanceLosses = [], []

        objects = list(goalGraph.objects())
        objectEncodings = self.objectEncoder(spec, [o.execute() for o in objects])
        object2encodingIndex = {o: i for i,o in enumerate(objects) }
        
        specEncoding = self.specEncoder(spec)

        while True:
            optimalMoves = list(currentGraph.policyOracle(goalGraph))
            if len(optimalMoves) == 0:
                finalMove = True
                optimalMoves = [['RETURN']]
            else:
                finalMove = False

            # Gather together objects in scope
            objectsInScope = list(currentGraph.objects())
            if len(currentGraph) == 0:
                scopeEncoding = None
            else:
                scopeEncoding = objectEncodings[self.tensor([object2encodingIndex[o]
                                                             for o in objectsInScope ])]

            object2pointer = {o: Pointer(i)
                              for i, o  in enumerate(objectsInScope)}

            h0 = self.initialHidden(scopeEncoding, specEncoding)
            def substitutePointers(serialization):
                return [token if isinstance(token,str) else object2pointer[token]
                        for token in serialization]

            targetLines = [substitutePointers(m.serialize()) if not finalMove else m
                           for m in optimalMoves]
            targetLikelihoods = [self.decoder.logLikelihood(h0, targetLine, scopeEncoding)
                                 for targetLine in targetLines]
            policyLoss = -torch.logsumexp(torch.cat([l.view(1) for l in targetLikelihoods ]), dim=0)
            policyLosses.append(policyLoss)

            actualDistance = currentGraph.distanceOracle(goalGraph)
            predictedDistance = self.distance(scopeEncoding, specEncoding)
            distanceLoss = (predictedDistance - float(actualDistance))**2

            # On-policy sample
            onPolicy = self.decoder.sample(h0, scopeEncoding)
            if onPolicy is not None:
                onPolicy = [objectsInScope[token.i] if isinstance(token, Pointer) else token
                            for token in onPolicy]
                onPolicy = self.DSL.parseLine(onPolicy)
                if onPolicy is not None:
                    onPolicyGraph = currentGraph.extend(onPolicy)
                    actualDistance = onPolicyGraph.distanceOracle(goalGraph)
                    if onPolicy in goalGraph.nodes:
                        # Use precomputed on policy embeddings
                        newObjectEncoding = objectEncodings[object2encodingIndex[onPolicy]]
                    else:
                        # Annoyingly have to compute new embedding
                        newObjectEncoding = self.objectEncoder(spec, onPolicy.execute())
                    if len(objectsInScope) > 0: # stuck it on to the objects we already have
                        onPolicyObjects = torch.cat([scopeEncoding, newObjectEncoding.view(1,-1)])
                    else: # it is its own thing
                        onPolicyObjects = torch.stack([newObjectEncoding])                        
                        
                    predictedDistance = self.distance(onPolicyObjects, specEncoding)
                    distanceLoss += (predictedDistance - float(actualDistance))**2                

            
            policyLosses.append(policyLoss.data.item())
            distanceLosses.append(distanceLoss.data.item())

            if finalMove:
                (sum(policyLosses) + sum(distanceLosses)).backward()
                optimizer.step()
                return policyLosses, distanceLosses

            # Sample the next (optimal) line of code predicted by the model
            targetLikelihoods = [math.exp(tl.data.item() + policyLoss) for tl in targetLikelihoods]
            # This shouldn't be necessary - normalize :/
            targetLikelihoods = [tl/sum(targetLikelihoods) for tl in targetLikelihoods ]
            move = np.random.choice(optimalMoves, p=targetLikelihoods)
            currentGraph = currentGraph.extend(move)
            


        
        
        while True:
            self.zero_grad()
        
            optimalMoves = list(currentGraph.policyOracle(goalGraph))
            if len(optimalMoves) == 0:
                finalMove = True
                optimalMoves = [['RETURN']]
            else:
                finalMove = False

            objects = list(currentGraph.objects())
            object2pointer = {o: Pointer(i)
                              for i,o in enumerate(objects) }

            if len(objects) > 0:
                objectEncodings = torch.stack([ self.objectEncoder(spec, o.execute())
                                                for o in objects])
            else:
                objectEncodings = self.device(torch.zeros((1, self.objectEncoder.outputDimensionality)))

            se = self.specEncoder(spec)
            h0 = self.initialHidden(objectEncodings, se)

            def substitutePointers(serialization):
                return [token if isinstance(token,str) else object2pointer[token]
                        for token in serialization]

            targetLines = [substitutePointers(m.serialize()) if not finalMove else m
                           for m in optimalMoves]
            targetLikelihoods = [self.decoder.logLikelihood(h0, targetLine, objectEncodings if len(objects) > 0 else None)
                                 for targetLine in targetLines]
            policyLoss = -torch.logsumexp(torch.cat([l.view(1) for l in targetLikelihoods ]), dim=0)

            actualDistance = currentGraph.distanceOracle(goalGraph)
            predictedDistance = self.distance(objectEncodings, se)
            distanceLoss = (predictedDistance - float(actualDistance))**2

            # On-policy sample
            onPolicy = self.decoder.sample(h0, objectEncodings if len(objects) > 0 else None)
            if onPolicy is not None:
                onPolicy = [objects[token.i] if isinstance(token, Pointer) else token
                            for token in onPolicy]
                onPolicy = self.DSL.parseLine(onPolicy)
                if onPolicy is not None:
                    onPolicyGraph = currentGraph.extend(onPolicy)
                    actualDistance = onPolicyGraph.distanceOracle(goalGraph)
                    newObjectEncoding = self.objectEncoder(spec, onPolicy.execute())
                    if len(objects) > 0: # stuck it on to the objects we already have
                        onPolicyObjects = torch.cat([objectEncodings, newObjectEncoding.view(1,-1)])
                    else: # it is its own thing
                        onPolicyObjects = torch.stack([newObjectEncoding])                        
                        
                    predictedDistance = self.distance(onPolicyObjects, se)
                    distanceLoss += (predictedDistance - float(actualDistance))**2                

            
            (policyLoss + distanceLoss).backward()
            optimizer.step()
            policyLosses.append(policyLoss.data.item())
            distanceLosses.append(distanceLoss.data.item())

            if finalMove: return policyLosses, distanceLosses

            # Sample the next (optimal) line of code predicted by the model
            targetLikelihoods = [math.exp(tl.data.item() + policyLoss) for tl in targetLikelihoods]
            # This shouldn't be necessary - normalize :/
            targetLikelihoods = [tl/sum(targetLikelihoods) for tl in targetLikelihoods ]
            move = np.random.choice(optimalMoves, p=targetLikelihoods)
            currentGraph = currentGraph.extend(move)
            

    def sample(self, spec, maxMoves=None):
        specEncoding = self.specEncoder(spec)
        objectEncodings = {}

        graph = ProgramGraph([])

        while True:
            # Make the encoding matrix
            if len(objectEncodings) > 0:
                objects = list(objectEncodings.keys())
                oe = torch.stack([ objectEncodings[o]
                                   for o in objects ])
            else:
                oe = self.device(torch.zeros((1, self.objectEncoder.outputDimensionality)))
            h0 = self.initialHidden(oe, specEncoding)

            nextLineOfCode = self.decoder.sample(h0, oe if len(objectEncodings) > 0 else None)
            nextLineOfCode = [objects[t.i] if isinstance(t, Pointer) else t
                              for t in nextLineOfCode ]

            if 'RETURN' in nextLineOfCode or len(graph) >= maxMoves: return graph

            nextObject = self.DSL.parseLine(nextLineOfCode)
            if nextObject is None: return None

            graph = graph.extend(nextObject)
            objectEncodings[nextObject] = self.objectEncoder(spec, nextObject.execute())

    def repeatedlySample(self, specEncoding, graph, objectEncodings, n_samples):
        """Repeatedly samples a single line of code.
        specEncoding: Encoding of the spec
        objectEncodings: dictionary mapping object to it's encoding
        graph: the current graph
        n_samples: how many samples to draw
        returns: list of sampled DSL objects. If the sample is `RETURN` then that entry in the list is None.
        """

        objectsInScope = list(graph.objects())
        if len(graph) > 0:
            oe = torch.stack([ objectEncodings[o]
                               for o in objectsInScope ])
        else:
            oe = self.device(torch.zeros((1, self.objectEncoder.outputDimensionality)))
        
        h0 = self.initialHidden(oe, specEncoding)

        samples = []
        for _ in range(n_samples):
            nextLineOfCode = self.decoder.sample(h0, oe if len(objectsInScope) > 0 else None)
            if nextLineOfCode is None: continue
            nextLineOfCode = [objectsInScope[t.i] if isinstance(t, Pointer) else t
                              for t in nextLineOfCode ]
            if 'RETURN' in nextLineOfCode:
                samples.append(None)
            else:
                nextObject = self.DSL.parseLine(nextLineOfCode)
                if nextObject is not None:
                    samples.append(nextObject)

        return samples





