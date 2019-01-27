from programGraph import *

class SMC():
    def __init__(self, model, _=None,
                 particles=0, fitnessWeight=2.):
        assert particles > 0, "You must specify a number of particles"
        self.particles = particles
        self.fitnessWeight = fitnessWeight
        self.model = model

    def infer(self, spec, maximumLength=8):
        specEncoding = self.model.specEncoder(spec)
        
        # Maps from an object to its embedding
        _objectEncodings = {}
        def objectEncoding(o):
            if o in _objectEncodings: return _objectEncodings[o]
            oe = self.model.objectEncoder(spec, o.execute())
            _objectEncodings[o] = oe
            return oe

        def scopeEncoding(g):
            if len(g) > 0:
                return torch.stack([objectEncoding(o)
                                    for o in g.objects() ])
            else:
                return self.model.device(torch.zeros((1, self.model.objectEncoder.outputDimensionality)))
            
        # Maps from a graph to its distance
        _distance = {}
        def distance(g):
            if g in _distance: return _distance[g]
            se = scopeEncoding(g)
            d = self.model.distance(se, specEncoding)
            _distance[g] = d
            return d            
        
        class Particle():
            def __init__(self, graph, frequency):
                self.frequency = frequency
                self.graph = graph
                self.distance = distance(graph)
            
        
        population = [Particle(ProgramGraph([]), self.particles)]

        for _ in range(maximumLength):
            sampleFrequency = {}
            for p in population:
                for newObject in self.model.repeatedlySample(specEncoding, p.graph,
                                                             _objectEncodings, p.frequency):
                    if newObject is not None: objectEncoding(newObject)
                    if newObject is None: newGraph = p.graph
                    else: newGraph = p.graph.extend(newObject)                        
                    sampleFrequency[newGraph] = sampleFrequency.get(newGraph, 0) + 1

            # Convert graphs to particles
            samples = [Particle(g, f)
                       for g, f in sampleFrequency.items() ]

            # Resample
            logWeights = [math.log(p.frequency) - p.distance
                          for p in samples]
            ps = [ math.exp(lw - max(logWeights)) for lw in logWeights ]
            ps = [p/sum(ps) for p in ps]
            sampleFrequencies = np.random.multinomial(self.particles, ps)
            
            population = []
            for particle, frequency in zip(samples, sampleFrequencies):
                if frequency > 0:
                    particle.frequency = frequency
                    population.append(particle)

        return [p.graph for p in population]
